"""The icon pipeline must ship an icon that still has its transparency.

Rant 2026-10-13T17:45:42 (host: after installing 0.3.10 the app icon had a white
ring around it), measured on the shipped artifact rather than inferred:

* `packaging/assets/icon.png` was `1024x1024, 8-bit/color RGB` — **no alpha
  channel at all** — so the PNG was opaque white behind the rounded shape;
* `icon-512.png`, every entry of `icon.icns` and every size of `icon.ico` were
  equally opaque (corners `#FFFFFF`, alpha 255), and macOS 26 — which clips the
  icon into its own rounded tile — showed the white as a border.

Two defects in `packaging/gen-assets.sh`:

1. the Chrome fallback screenshotted `icon.svg` inside a page painted
   `background:#fff`, so every transparent region of the design was baked to
   white and the screenshot was written without alpha. The page is now
   `background:transparent` and the render is passed
   `--default-background-color=00000000`;
2. the iconset loop rendered 16/32/128/256/512 and then `cp`'d each 1x file onto
   its own `@2x` name. `iconutil` validates an entry's pixel size against its
   name and silently drops a mismatch, so the shipped `.icns` had no
   `icon_512x512@2x` (1024 — the `ic10` entry) and its largest image was 512.
   Every entry is now rendered at its real size.

Neither was visible to the check the script already had: it only ever refused a
render that was *too transparent* — a PNG with no alpha channel was answered
"assumed opaque" — which is exactly how a white icon passed every gate.

The arms read the **artifact the script produced**, not the script's text: a text
assertion would pass on a script that still renders white. The generator is run
from a copy of the tree with stand-in renderers on `PATH` (the shape
`tests/test_release_notarize_classifies_its_failure.py` established), and
`check_icon_opaque`'s body is extracted and driven directly for the reverse
direction, so both arms measure behaviour on a tree the caller's host cannot
influence.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packaging" / "gen-assets.sh"

sys.path.insert(0, str(REPO / "packaging"))
import pngutil  # noqa: E402

# The whole file runs the generator, which is a POSIX shell script; without a real
# bash the run measures the shell's absence, not the script.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the subject is a POSIX shell script that these arms execute; on Windows the run "
        "would measure the shell's absence. The generator itself is macOS/Linux-only in "
        "the release matrix's shell steps."
    ),
)

ICON = 1024
MARGIN = 56  # icon.svg insets its rounded rect by 56px on a 1024 canvas

# ── stand-ins ────────────────────────────────────────────────────────────────
# `#!/usr/bin/env python3` and `#!/bin/sh`, so the same files serve macOS and the
# ubuntu leg. Each is written into a directory prepended to PATH.

_RSVG_STUB = """#!/bin/sh
# A renderer that produced nothing usable — the v0.2.29 shape, where librsvg
# rendered the icon 99.2% transparent. The script must fall through to the next
# candidate, which is the path these arms exercise.
exit 1
"""

_SIPS_STUB = """#!/bin/sh
# macOS `sips` is present but never produces an accepted render here, so a
# fallback chain can only end in the script's own error rather than in a real
# last-resort render this test does not control.
exit 1
"""

_CHROME_STUB = '''#!/usr/bin/env python3
"""Stand in for Chrome headless, keyed on the page it is asked to screenshot.

Faithful in the one respect these arms are about: a page carrying an opaque
background is screenshotted with that background baked in and written without an
alpha channel (colortype 2), while a transparent page plus
--default-background-color=00000000 keeps the SVG's own transparency (colortype
6 with transparent corners). That is the difference the real Chrome showed
between the wrapper before rant 2026-10-13T17:45:42 and the fixed one — the
shipped `icon.png` really was colortype 2 with `#FFFFFF` corners.

It records the argv and the page it was handed before deciding anything, so an
arm can read what the script asked for even when the run then fails.
"""
import json, os, struct, sys, zlib

argv = sys.argv[1:]
shot = next((a.split("=", 1)[1] for a in argv if a.startswith("--screenshot=")), None)
with open(argv[-1][len("file://"):], encoding="utf-8") as fh:
    page = fh.read()
transparent_page = "background:transparent" in page
transparent_flag = "--default-background-color=00000000" in argv
with open(os.environ["STUB_CHROME_RECORD"], "w", encoding="utf-8") as fh:
    json.dump({"argv": argv, "page": page, "transparent_page": transparent_page,
               "transparent_flag": transparent_flag}, fh)
if shot is None:
    sys.exit("stub chrome: no --screenshot argument")
flattened = not (transparent_page and transparent_flag)


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(
        ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write(path, colortype, rows):
    raw = b"".join(b"\\x00" + row for row in rows)
    ihdr = struct.pack(">IIBBBBB", 1024, 1024, 8, colortype, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", ihdr)
                 + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if flattened:
    write(shot, 2, [b"\\xff\\xff\\xff" * 1024] * 1024)
else:
    clear = bytes((0, 0, 0, 0)) * 1024
    painted = bytes((26, 32, 44, 255)) * (1024 - 2 * 56)
    middle = bytes((0, 0, 0, 0)) * 56 + painted + bytes((0, 0, 0, 0)) * 56
    write(shot, 6, [clear] * 56 + [middle] * (1024 - 2 * 56) + [clear] * 56)
'''

_ICONUTIL_STUB = '''#!/usr/bin/env python3
"""Stand in for macOS iconutil: record the iconset entries it was handed.

The real iconutil silently DROPS an entry whose pixel size does not match its
name — the defect guarded here (a 512px copy under `icon_512x512@2x.png`, so the
shipped .icns carried no 1024 entry at all). Recording the sizes makes that
observable on the Linux leg too, where the real iconutil does not exist.
"""
import json, os, struct, sys

argv = sys.argv[1:]
iconset = argv[argv.index("-c") + 2]
out = argv[argv.index("-o") + 1]
entries = {}
for name in sorted(os.listdir(iconset)):
    with open(os.path.join(iconset, name), "rb") as fh:
        header = fh.read(24)
    entries[name] = list(struct.unpack(">II", header[16:24]))
with open(os.environ["STUB_ICONUTIL_RECORD"], "w", encoding="utf-8") as fh:
    json.dump(entries, fh)
with open(out, "wb") as fh:
    fh.write(b"icns")
'''

_STUBS = (
    ("rsvg-convert", _RSVG_STUB),
    ("sips", _SIPS_STUB),
    ("google-chrome", _CHROME_STUB),
    ("iconutil", _ICONUTIL_STUB),
)


def _render(tmp_path):
    """Run the generator from a copy of the tree, with the stand-ins on PATH."""
    shell = shutil.which("bash")
    if shell is None:  # pragma: no cover - a POSIX-less host
        pytest.skip("no POSIX shell is available to run the generator")
    if shutil.which("python3") is None:  # pragma: no cover - the script drives python3
        pytest.skip("gen-assets.sh drives python3; without it the run measures the host")

    tree = tmp_path / "tree"
    shutil.copytree(REPO / "packaging", tree / "packaging")
    script = tree / "packaging" / "gen-assets.sh"
    assert script.is_file(), "the generator is missing from the copied tree"

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, source in _STUBS:
        stub = bindir / name
        stub.write_text(source, encoding="utf-8")
        stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env.pop("CHROME_BIN", None)  # the stub only wins if the script resolves it from PATH
    env["STUB_CHROME_RECORD"] = str(tmp_path / "chrome.json")
    env["STUB_ICONUTIL_RECORD"] = str(tmp_path / "iconutil.json")

    result = subprocess.run(
        [shell, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",  # the generator prints UTF-8 arrows and warnings
        timeout=600,
    )
    return result, tree / "packaging" / "assets", tmp_path / "chrome.json", tmp_path / "iconutil.json"


def _record(path: Path):
    """What a stand-in recorded, or `None` when its step never ran.

    `None` is a state to report, not to crash on: when a render is refused the
    script may never reach the iconset step, and the arms below say so with the
    run's own output instead of a `FileNotFoundError`.
    """
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One generator run, shared by the arms — the resize helpers are the slow part."""
    return _render(tmp_path_factory.mktemp("gen-assets"))


def _corners(name, path):
    w, h, bpp, px = pngutil.read_png(str(path))
    assert bpp == 4, (
        f"{name} has NO alpha channel — the render flattened the transparent background "
        "into a solid colour, which is the white ring of rant 2026-10-13T17:45:42"
    )
    for corner, (x, y) in (
        ("top-left", (0, 0)),
        ("top-right", (w - 1, 0)),
        ("bottom-left", (0, h - 1)),
        ("bottom-right", (w - 1, h - 1)),
    ):
        alpha = px[(y * w + x) * 4 + 3]
        assert alpha == 0, (
            f"{name}'s {corner} corner is opaque (alpha={alpha}) — a background was baked "
            "into the render; the design has none, so macOS shows it as a border"
        )


def test_the_rendered_assets_keep_the_design_transparency(rendered):
    result, assets, _chrome_path, _icns_path = rendered
    assert result.returncode == 0, (
        f"the generator failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    for name in ("icon.png", "icon-512.png", "icon-256.png"):
        path = assets / name
        assert path.is_file(), f"{name} was not produced"
        _corners(name, path)


def test_chrome_is_asked_for_a_transparent_page(rendered):
    """The mechanism, read from what the renderer was handed.

    This is the arm that stays red under the mutation the rant names — paint the
    wrapper page white again — without depending on where the fallback chain
    happens to stop on the host running it: the stand-in records its argv and the
    page it was given, so the reading is of what the script asked for.
    """
    result, _assets, chrome_path, _icns_path = rendered
    chrome = _record(chrome_path)
    assert chrome is not None, (
        "the Chrome fallback never ran, so what the script asked it for is unmeasured; "
        f"the run said:\n{result.stdout}\n{result.stderr}"
    )
    assert chrome["transparent_page"], (
        f"the wrapper page is not transparent — an opaque page is what baked the white "
        f"in: {chrome['page']!r}"
    )
    assert chrome["transparent_flag"], (
        "Chrome was not passed --default-background-color=00000000; the page alone does "
        "not switch the screenshot's background off (rant 2026-08-12T17:25:28 measured "
        f"the flag as the switch): {chrome['argv']!r}"
    )


def _expected_size(name: str) -> int:
    """The pixel size an iconset entry's name requires (an `@2x` slot is twice its 1x)."""
    stem = name[len("icon_"):-len(".png")]
    scale = 2 if stem.endswith("@2x") else 1
    return int(stem.split("@")[0].split("x")[0]) * scale


def test_the_icns_is_handed_every_entry_at_its_real_size(rendered):
    result, _assets, _chrome_path, icns_path = rendered
    icns = _record(icns_path)
    assert icns is not None, (
        "the generator never reached the iconset step, so iconutil was handed nothing to "
        f"measure; the run said:\n{result.stdout}\n{result.stderr}"
    )
    assert icns, "iconutil was handed an empty iconset — the arm would measure nothing"
    assert "icon_512x512@2x.png" in icns, f"no 512@2x entry was rendered: {sorted(icns)}"
    for name, (w, h) in icns.items():
        expected = _expected_size(name)
        assert (w, h) == (expected, expected), (
            f"{name} was handed {w}x{h}, but its name requires {expected}x{expected} — "
            "iconutil drops a mismatch, which is how the shipped .icns lost its 1024"
        )
    assert max(w for w, _h in icns.values()) == ICON, (
        f"the largest entry is {max(w for w, _h in icns.values())}px, not {ICON} — the "
        ".icns would carry no ic10 entry"
    )


# ── the check's own reverse direction (rant item 2) ──────────────────────────


def _guard_body() -> str:
    """`check_icon_opaque`'s python body, so the check can be driven directly.

    Scoped to the function's own region — the file holds four `PYEOF` heredocs, so
    a document-wide search could extract a different one. Extraction failure is
    asserted rather than tolerated: an arm that silently measured nothing would
    pass, which is the failure mode this whole test file exists to prevent.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.strip(), "gen-assets.sh is empty"
    region = text[text.index("check_icon_opaque() {"):]
    region = region[: region.index("\n}\n")]
    marker = "<<'PYEOF'\n"
    body = region[region.index(marker) + len(marker):]
    body = body[: body.index("\nPYEOF")]
    assert "opaque_ratio" in body and "sys.exit(1)" in body, (
        "the extracted region is not the icon check — the arm would measure nothing"
    )
    return body


def _png_bytes(w: int, h: int, colortype: int, rows: list[bytes]) -> bytes:
    """A PNG of the given colour type — colortype 2 is the shape this arm needs.

    `pngutil` writes colortype 6 only, and "the render lost its alpha channel" is
    precisely a colortype-2 PNG, so the arm has to be able to make one.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, colortype, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _icon_rows(w: int, h: int, corner_alpha: int) -> list[bytes]:
    """Rows of a rendered icon: a painted centre, corners at `corner_alpha`."""
    edge = bytes((255, 255, 255, corner_alpha)) * w
    painted = bytes((26, 32, 44, 255)) * (w - 2 * MARGIN)
    middle = bytes((255, 255, 255, corner_alpha)) * MARGIN + painted
    middle += bytes((255, 255, 255, corner_alpha)) * MARGIN
    return [edge] * MARGIN + [middle] * (h - 2 * MARGIN) + [edge] * MARGIN


def _run_guard(body: str, png: bytes, tmp_path) -> subprocess.CompletedProcess:
    target = tmp_path / "icon.png"
    target.write_bytes(png)
    script = tmp_path / "check_icon_opaque.py"
    script.write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env["PNGUTIL_DIR"] = str(REPO / "packaging")
    return subprocess.run(
        [sys.executable, str(script), str(target)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def test_the_check_refuses_a_render_that_lost_its_transparency(tmp_path):
    """Both directions, so neither arm passes vacuously.

    The check used to answer "assumed opaque" for a PNG with no alpha channel, and
    to accept any render that was opaque enough — so the white icon passed every
    gate. Without the third arm below, a check that rejected everything would look
    just as green.
    """
    body = _guard_body()

    accepted = _run_guard(body, _png_bytes(ICON, ICON, 6, _icon_rows(ICON, ICON, 0)), tmp_path)
    assert accepted.returncode == 0, (
        f"a correct RGBA render was refused:\n{accepted.stdout}\n{accepted.stderr}"
    )
    assert "corners" in accepted.stdout, (
        f"the check accepted the render without reading its corners:\n{accepted.stdout}"
    )

    flattened = _run_guard(
        body, _png_bytes(ICON, ICON, 2, [bytes((255, 255, 255)) * ICON] * ICON), tmp_path
    )
    assert flattened.returncode == 1, (
        f"a render with no alpha channel at all was accepted:\n{flattened.stdout}"
    )
    assert "NO alpha channel" in flattened.stderr, flattened.stderr

    baked = _run_guard(body, _png_bytes(ICON, ICON, 6, _icon_rows(ICON, ICON, 255)), tmp_path)
    assert baked.returncode == 1, (
        f"a render whose corners are opaque was accepted:\n{baked.stdout}"
    )
    assert "corner is opaque" in baked.stderr, baked.stderr
