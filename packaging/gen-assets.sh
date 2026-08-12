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
# Renderer priority: rsvg-convert → Chrome/Chromium headless → sips.
# ⚡ v0.2.29 CI lesson (Build Release 31604108964): rsvg-convert (librsvg 2.58 on
#    Ubuntu 24.04 / Homebrew / Windows) renders icon.svg 99.2% transparent — even
#    the opaque background rect does not paint. The #705 opacity gate correctly
#    caught it, but the script exited instead of falling back to the next
#    renderer. Fix: each renderer's output is validated by the opacity check
#    BEFORE being accepted; a blank render falls through to the next renderer.
#    Only when ALL renderers produce blank output do we fail loudly.
render_svg_to_png() {
  local renderer_found=0
  # 1) rsvg-convert (librsvg) — full SVG + filter support
  if command -v rsvg-convert >/dev/null 2>&1; then
    renderer_found=1
    echo "    renderer: rsvg-convert"
    if rsvg-convert -w 1024 -h 1024 "$SVG" -o "$OUT/icon.png" \
        && check_icon_opaque; then
      return 0
    fi
    echo "    ⚠️ rsvg-convert produced a blank/transparent icon — falling back to Chrome headless" >&2
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
      renderer_found=1
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
      if "$chrome" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
          --allow-file-access-from-files \
          --screenshot="$OUT/icon.png" --window-size=1024,1024 \
          "$PAGE_URL" >/dev/null 2>&1 \
          && check_icon_opaque; then
        rm -f "$HTML"
        return 0
      fi
      rm -f "$HTML"
      echo "    ⚠️ $chrome produced a blank/transparent icon — trying next renderer" >&2
    fi
  done
  # 3) sips (macOS last resort) — 辉光（feGaussianBlur）可能丢失
  if command -v sips >/dev/null 2>&1; then
    renderer_found=1
    echo "    ⚠️ renderer: sips (last resort) — glow may be lost (feGaussianBlur unsupported)" >&2
    if sips -s format png "$SVG" --out "$OUT/icon.png" >/dev/null 2>&1 \
        && check_icon_opaque; then
      return 0
    fi
    echo "    ⚠️ sips produced a blank/transparent icon — all renderers failed" >&2
  fi
  if [ "$renderer_found" -eq 0 ]; then
    echo "ERROR: no SVG renderer found (rsvg-convert / chrome / sips)" >&2
  fi
  return 1
}

# ⚡ transparency check (rant 2026-08-12T17:25:28 + v0.2.29 CI lesson): Chrome
#    headless / rsvg-convert can silently produce a fully-transparent PNG (SVG
#    not painted). Fail loudly instead of shipping a blank icon. Sample the
#    alpha channel; if >90% of pixels are transparent, the renderer failed.
#    Returns 0 when the icon is opaque enough (accepted), 1 when blank (caller
#    falls back to the next renderer).
#    ⚡ Filter-aware decode (v0.2.29 Build Release lesson): rsvg-convert /
#    Chrome emit adaptively-filtered PNG rows (Sub/Up/Average/Paeth). A naive
#    "strip filter byte" read returns deltas, not pixels — a fully opaque icon
#    falsely read as "99.2% transparent" (and the area-average resize produced
#    garbage hues). pngutil.read_png reverses filters, so the check sees real
#    alpha and the derived icon-512/256/icns/ico are correct.
check_icon_opaque() {
  python3 - "$OUT/icon.png" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import pngutil

w, h, bpp, px = pngutil.read_png(sys.argv[1])
if (w, h) != (1024, 1024):
    sys.stderr.write(f"ERROR: expected 1024x1024 PNG, got {w}x{h} — renderer failed\n")
    sys.exit(1)
print(f"    icon.png OK: {w}x{h}")

if bpp == 4:
    ratio = pngutil.opaque_ratio(w, h, bpp, px)
    print(f"    icon.png alpha: {ratio:.1%} opaque")
    if ratio < 0.10:
        sys.stderr.write(
            f"ERROR: icon.png is {1-ratio:.1%} transparent — renderer failed to paint "
            "the SVG. Falling back to next renderer (see rant 2026-08-12T17:25:28).\n"
        )
        sys.exit(1)
else:
    print(f"    icon.png alpha: colortype={'RGB' if bpp == 3 else bpp} (no alpha channel) — assumed opaque")
PYEOF
}

render_svg_to_png || exit 1

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
