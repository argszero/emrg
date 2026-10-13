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
  # ⚠️ rant 2026-08-12T17:25:28: screenshotting the raw file:// SVG yields an unpainted
  #    (fully transparent) image, so the SVG is wrapped in a local HTML <img> page.
  #    That first wrapper painted an OPAQUE WHITE page and passed no background flag —
  #    which baked the white into the render and shipped a ring around the icon; the
  #    current wrapper is the one described at the renderer below (rant
  #    2026-10-13T17:45:42).
  #    The page background and the flag each do half the job. Measured on Chrome
  #    154.0.8037.99 over all four combinations (page transparent vs #fff × flag
  #    present vs absent): a transparent page alone still renders opaque RGB, and the
  #    flag alone over a white page is a no-op — because
  #    `--default-background-color` paints whatever the *page* leaves transparent, and
  #    a white page leaves nothing. BOTH are required: a transparent wrapper page AND
  #    --default-background-color=00000000. Also add --allow-file-access-from-files so
  #    the file:// img can load from the file:// page. The checks in check_icon_opaque
  #    then fail loudly in both directions — a blank/transparent render, and a
  #    flattened (no-alpha or opaque-corner) one.
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
      echo "    renderer: $chrome (headless, HTML wrapper, transparent bg)"
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
      # ⚡ rant 2026-10-13T17:45:42 (host: the app icon has a white ring around it):
      #    this wrapper used to paint an OPAQUE WHITE page, so every transparent
      #    region of the SVG was baked to white and the screenshot was written
      #    with no alpha channel at all (icon.png came out colortype 2/RGB,
      #    1024x1024, corners #FFFFFF). Everything derived from it inherited the
      #    white — icon-512/256, every entry of icon.icns, every size of
      #    icon.ico — and macOS 26, which clips the icon into its own rounded
      #    tile, showed it as a white border around the shape.
      #    A transparent page plus the matching --default-background-color (see
      #    the invocation below) keeps the SVG's own transparency, and
      #    check_icon_opaque now refuses a flattened or alpha-less render so the
      #    white cannot come back unnoticed.
      cat > "$HTML" <<HTMLEOF
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>html,body{margin:0;padding:0;background:transparent}</style></head>
<body><img src="$SVG_URL" width="1024" height="1024"></body></html>
HTMLEOF
      TIMEOUT_CMD=""
      command -v timeout >/dev/null 2>&1 && TIMEOUT_CMD="timeout 90"
      NO_SANDBOX=""
      # v0.2.55 Build Release lesson: on ubuntu-24.04 x86_64 the Chrome headless
      # fallback hung forever (no step-level timeout in CI), blocking the whole
      # release build twice. Add a hard timeout so a stuck renderer falls through
      # to the next renderer, and --no-sandbox on Linux (runner container).
      [ "$(uname -s)" = "Linux" ] && NO_SANDBOX="--no-sandbox"
      if $TIMEOUT_CMD "$chrome" --headless --disable-gpu --hide-scrollbars \
          --force-device-scale-factor=1 --allow-file-access-from-files \
          $NO_SANDBOX \
          --default-background-color=00000000 \
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
#    ⚡ The opposite direction is checked too (rant 2026-10-13T17:45:42): a
#    render that lost its alpha channel, or whose corners are opaque, has a
#    background baked in and is rejected the same way — see the rules at the
#    end of the body.
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

# ⚡ rant 2026-10-13T17:45:42 — the gap this closes: every rule above only ever
#    refused a render that was TOO transparent, so one that flattened the
#    transparent canvas to opaque white passed every gate and shipped. The two
#    rules below are the reverse direction: the render must still have an alpha
#    channel, and the design's corners must still be see-through.
if bpp != 4:
    sys.stderr.write(
        "ERROR: icon.png has NO alpha channel (colortype 2/RGB) — the render "
        "flattened the transparent background to a solid colour, which macOS then "
        "shows as a border around the icon once it clips it into a rounded tile. "
        "Falling back to next renderer (gap closed for rant 2026-10-13T17:45:42).\n"
    )
    sys.exit(1)

ratio = pngutil.opaque_ratio(w, h, bpp, px)
print(f"    icon.png alpha: {ratio:.1%} opaque")
if ratio < 0.10:
    sys.stderr.write(
        f"ERROR: icon.png is {1-ratio:.1%} transparent — renderer failed to paint "
        "the SVG. Falling back to next renderer (see rant 2026-08-12T17:25:28).\n"
    )
    sys.exit(1)

# icon.svg paints a rounded rect inset from the canvas and carries no background
# of its own, so all four corners of a correct render are fully transparent. An
# opaque corner means a background colour was baked in (the white ring).
CORNERS = (("top-left", 0, 0), ("top-right", w - 1, 0),
           ("bottom-left", 0, h - 1), ("bottom-right", w - 1, h - 1))
for name, cx, cy in CORNERS:
    o = (cy * w + cx) * 4
    if px[o + 3] != 0:
        sys.stderr.write(
            f"ERROR: icon.png's {name} corner is opaque (alpha={px[o + 3]}, "
            f"rgb={tuple(px[o:o + 3])}) — a background was baked into the render. "
            "Falling back to next renderer (rant 2026-10-13T17:45:42).\n"
        )
        sys.exit(1)
print("    icon.png corners: all four transparent")
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
  # ⚡ rant 2026-10-13T17:45:42: this block used to render 16/32/128/256/512 and
  #    then `cp` each 1x file onto its own `@2x` name. iconutil validates an
  #    entry's pixel size against its name and SILENTLY DROPS a mismatch, so
  #    those copies never landed: the shipped icon.icns had no
  #    icon_512x512@2x (1024 — the ic10 entry) and no icon_32x32@2x (64), and
  #    its largest image was 512. Every entry is now rendered at its real size.
  python3 - "$OUT/icon.png" "$ICONSET" <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("PNGUTIL_DIR", os.path.join(os.path.dirname(sys.argv[1]), "..")))
import pngutil

src, iconset = sys.argv[1], sys.argv[2]
w, h, bpp, px = pngutil.read_png(src)
rgba = pngutil.to_rgba(w, h, bpp, px)

# The iconset's fixed set: each @2x slot is double the pixels of its 1x slot.
for name, size in (
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
):
    if size > w:
        continue
    scaled = rgba if size == w else pngutil.resize_area(rgba, w, h, size, size)
    pngutil.write_png(f"{iconset}/{name}", size, size, scaled)
    print(f"    {name}: {size}x{size}")
PYEOF
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
