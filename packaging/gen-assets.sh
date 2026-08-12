#!/usr/bin/env bash
# EMRG Phase 4 — generate packaging assets (icons) from the SVG design source.
#
# Design source: packaging/assets/icon.svg  ("Branch Emergence" — rant 2026-08-11T18:28:09)
# Pipeline:
#   1. Render icon.svg → 1024x1024 icon.png
#      renderer priority: rsvg-convert (librsvg) → Chrome/Chromium headless → sips
#      (sips 对 feGaussianBlur 支持有限 → 辉光丢失，仅作最后兜底并告警)
#   2. iconutil (macOS) → .icns
#   3. Python stdlib (area-average box filter) → multi-size .ico (win)
#   4. 512/256 PNG copies (linux)
#
# Output in packaging/assets/:
#   icon.svg (design source, committed)  icon.png (1024)  icon-512.png
#   icon-256.png  icon.icns  icon.ico   (generated, gitignored)
#
# Run from repo root: bash packaging/gen-assets.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/packaging/assets"
SVG="$OUT/icon.svg"
# pngutil.py (filter-aware PNG decode) lives next to this script — the inline
# python heredocs below import it via PNGUTIL_DIR.
export PNGUTIL_DIR="$ROOT/packaging"
mkdir -p "$OUT"

if [ ! -f "$SVG" ]; then
  echo "ERROR: $SVG not found — design source missing" >&2
  exit 1
fi

echo "==> rendering icon.svg → 1024x1024 icon.png"
render_svg_to_png() {
  # 1) rsvg-convert (librsvg) — full SVG + filter support
  if command -v rsvg-convert >/dev/null 2>&1; then
    echo "    renderer: rsvg-convert"
    rsvg-convert -w 1024 -h 1024 "$SVG" -o "$OUT/icon.png"
    return 0
  fi
  # 2) Chrome/Chromium headless --screenshot (full filter support)
  # Windows (Git Bash): convert /c/... → file:///C:/... so Chrome resolves the URL.
  # ⚠️ rant 2026-08-12T17:25:28: two failure modes confirmed by local testing —
  #    (a) screenshotting the raw file:// SVG yields an unpainted (fully transparent)
  #        image; (b) `--default-background-color=00000000` alone makes Chrome emit an
  #        all-transparent PNG even from a working HTML wrapper (verified empirically:
  #        without the flag the same page paints correctly as opaque RGB).
  #    Fix: wrap the SVG in a local HTML <img> page with an OPAQUE white background,
  #    never pass the transparent-background flag, and add --allow-file-access-from-files
  #    so the file:// img can load from the file:// page. The transparency check below
  #    then fails loudly if a future change regresses to a blank render.
  SVG_URL="file://$SVG"
  if command -v cygpath >/dev/null 2>&1; then
    SVG_URL="file:///$(cygpath -m "$SVG")"
  fi
  for chrome in \
    "${CHROME_BIN:-}" \
    "google-chrome" "google-chrome-stable" "chromium" "chromium-browser" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"; do
    if [ -n "$chrome" ] && command -v "$chrome" >/dev/null 2>&1 || [ -x "$chrome" ] 2>/dev/null; then
      echo "    renderer: $chrome (headless, HTML wrapper, opaque white bg)"
      HTML="$OUT/.icon-render.html"
      # cygpath the wrapper page URL too — on Windows Git Bash $HTML is an MSYS
      # path (/c/Users/...), and file:///c/Users/... (missing drive colon) fails
      # to load in Chrome/Edge, silently rendering a blank opaque icon that the
      # alpha check below cannot catch (white wrapper bg → colortype 2 "assumed
      # opaque"). Same conversion as SVG_URL above. (empirically verified)
      PAGE_URL="file://$HTML"
      if command -v cygpath >/dev/null 2>&1; then
        PAGE_URL="file:///$(cygpath -m "$HTML")"
      fi
      cat > "$HTML" <<HTMLEOF
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:#fff}</style></head>
<body><img src="$SVG_URL" width="1024" height="1024"></body></html>
HTMLEOF
      "$chrome" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
        --allow-file-access-from-files \
        --screenshot="$OUT/icon.png" --window-size=1024,1024 \
        "$PAGE_URL" >/dev/null 2>&1
      rm -f "$HTML"
      return 0
    fi
  done
  # 3) sips (macOS last resort) — 辉光（feGaussianBlur）可能丢失
  if command -v sips >/dev/null 2>&1; then
    echo "    ⚠️ renderer: sips (last resort) — glow may be lost (feGaussianBlur unsupported)" >&2
    sips -s format png "$SVG" --out "$OUT/icon.png" >/dev/null 2>&1
    return 0
  fi
  echo "ERROR: no SVG renderer found (rsvg-convert / chrome / sips)" >&2
  return 1
}
render_svg_to_png || exit 1
# sanity: non-empty 1024x1024 PNG
python3 - "$OUT/icon.png" <<'PYEOF'
import sys, struct
data = open(sys.argv[1], "rb").read()
assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
pos = 8
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]
    tag = data[pos+4:pos+8]
    if tag == b"IHDR":
        w, h = struct.unpack(">II", data[pos+8:pos+16])
        assert (w, h) == (1024, 1024), f"expected 1024x1024, got {w}x{h}"
        print(f"    icon.png OK: {w}x{h}")
        break
    pos += 12 + ln
PYEOF

# ⚡ transparency check (rant 2026-08-12T17:25:28): Chrome headless can silently
#    produce a fully-transparent PNG (SVG not painted). Fail loudly instead of
#    shipping a blank icon. Sample the alpha channel; if >90% of pixels are
#    transparent, the renderer failed → exit 1.
#    ⚡ Filter-aware decode (v0.2.29 Build Release lesson): rsvg-convert /
#    Chrome emit adaptively-filtered PNG rows (Sub/Up/Average/Paeth). A naive
#    "strip filter byte" read returns deltas, not pixels — a fully opaque icon
#    falsely read as "99.2% transparent". Use pngutil.read_png (reverses
#    filters) so the check sees real alpha.
python3 - "$OUT/icon.png" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import pngutil

w, h, bpp, px = pngutil.read_png(sys.argv[1])
if bpp == 4:
    ratio = pngutil.opaque_ratio(w, h, bpp, px)
    print(f"    icon.png alpha: {ratio:.1%} opaque")
    if ratio < 0.10:
        sys.stderr.write(
            f"ERROR: icon.png is {1-ratio:.1%} transparent — renderer failed to paint "
            "the SVG. Fix the rsvg-convert/Chrome headless path (see rant 2026-08-12T17:25:28).\n"
        )
        sys.exit(1)
else:
    print(f"    icon.png alpha: colortype={'RGB' if bpp == 3 else bpp} (no alpha channel) — assumed opaque")
PYEOF

echo "==> resizing to 512/256 (stdlib area-average box filter)"
python3 - "$OUT/icon.png" "$OUT" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import pngutil

src, outdir = sys.argv[1], sys.argv[2]
w, h, bpp, px = pngutil.read_png(src)
rgba = pngutil.to_rgba(w, h, bpp, px)

for size in (512, 256):
    p = f"{outdir}/icon-{size}.png"
    pngutil.write_png(p, size, size, pngutil.resize_area(rgba, w, h, size, size))
    print("    wrote", p)
PYEOF

echo "==> iconutil → icon.icns (macOS only; skipped on Linux/Windows)"
if command -v iconutil >/dev/null 2>&1; then
  ICONSET="$OUT/icon.iconset"
  mkdir -p "$ICONSET"
  # iconutil wants specific sizes
  for s in 16 32 128 256 512; do
    python3 - "$OUT/icon.png" "$ICONSET/icon_${s}x${s}.png" "$s" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import pngutil

src, dst, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
w, h, bpp, px = pngutil.read_png(src)
rgba = pngutil.to_rgba(w, h, bpp, px)
pngutil.write_png(dst, size, size, pngutil.resize_area(rgba, w, h, size, size))
PYEOF
    cp "$ICONSET/icon_${s}x${s}.png" "$ICONSET/icon_${s}x${s}@2x.png" 2>/dev/null || true
  done
  # icns needs exact 16/32/128/256/512 + @2x
  iconutil -c icns "$ICONSET" -o "$OUT/icon.icns"
  rm -rf "$ICONSET"
  echo "    wrote $OUT/icon.icns"
else
  echo "    skipping icon.icns (iconutil unavailable — macOS only)"
fi

echo "==> icon.ico (multi-size, win)"
python3 - "$OUT/icon.png" "$OUT/icon.ico" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import struct
import pngutil

src, dst = sys.argv[1], sys.argv[2]
w, h, bpp, px = pngutil.read_png(src)
rgba = pngutil.to_rgba(w, h, bpp, px)

# ICO: header + directory + PNG-embedded entries
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [(s, pngutil.write_png_bytes(s, s, pngutil.resize_area(rgba, w, h, s, s))) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(imgs))
offset = 6 + 16 * len(imgs)
entries = b""
for s, png in imgs:
    entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0, 0, 0, 1, 32, len(png), offset)
    offset += len(png)
with open(dst, "wb") as f:
    f.write(header + entries + b"".join(p for _, p in imgs))
print("    wrote", dst)
PYEOF

echo "==> assets:"
ls -la "$OUT"
