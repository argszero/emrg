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
check_icon_opaque() {
  python3 - "$OUT/icon.png" <<'PYEOF'
import sys, struct, zlib

data = open(sys.argv[1], "rb").read()
assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
pos, w, h, idat, colortype = 8, 0, 0, b"", 0
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]
    tag = data[pos+4:pos+8]
    chunk = data[pos+8:pos+8+ln]
    if tag == b"IHDR":
        w, h = struct.unpack(">II", chunk[:8])
        colortype = chunk[9]
    elif tag == b"IDAT":
        idat += chunk
    pos += 12 + ln

if (w, h) != (1024, 1024):
    sys.stderr.write(f"ERROR: expected 1024x1024 PNG, got {w}x{h} — renderer failed\n")
    sys.exit(1)
print(f"    icon.png OK: {w}x{h}")

if colortype == 6:  # RGBA — only then is transparency meaningful
    raw = zlib.decompress(idat)
    stride0 = w * 4 + 1
    raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
    total = opaque = 0
    for y in range(0, h, 8):
        for x in range(0, w, 8):
            a = raw[y*w*4 + x*4 + 3]
            total += 1
            if a > 0:
                opaque += 1
    ratio = opaque / total
    print(f"    icon.png alpha: {ratio:.1%} opaque ({opaque}/{total} sampled)")
    if ratio < 0.10:
        sys.stderr.write(
            f"ERROR: icon.png is {1-ratio:.1%} transparent — renderer failed to paint "
            "the SVG. Falling back to next renderer (see rant 2026-08-12T17:25:28).\n"
        )
        sys.exit(1)
else:
    print(f"    icon.png alpha: colortype={colortype} (no alpha channel) — assumed opaque")
PYEOF
}

render_svg_to_png || exit 1

echo "==> resizing to 512/256 (stdlib area-average box filter)"
python3 - "$OUT/icon.png" "$OUT" <<'PYEOF'
import sys, zlib, struct

src, outdir = sys.argv[1], sys.argv[2]

def read_png(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, w, h, idat, ct = 8, 0, 0, b"", 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos+4])[0]
        tag = data[pos+4:pos+8]
        chunk = data[pos+8:pos+8+ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
            ct = chunk[9]
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    raw = zlib.decompress(idat)
    # PNG scanlines each have a leading filter byte (0 for None); strip them.
    # Chrome headless emits RGB (ct=2) when the page bg is opaque; rsvg-convert
    # emits RGBA (ct=6). Normalize to RGBA so downstream code can assume 4 bpp.
    if ct == 6:
        stride0 = w * 4 + 1
        raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
    elif ct == 2:
        stride0 = w * 3 + 1
        rows = [raw[y*stride0+1:(y+1)*stride0] for y in range(h)]
        out = bytearray()
        for row in rows:
            for i in range(0, len(row), 3):
                out += row[i:i+3] + b"\xff"
        raw = bytes(out)
    else:
        raise SystemExit(f"unsupported PNG colortype {ct}")
    return w, h, raw

w, h, raw = read_png(src)
stride = w * 4

def resize_area(nw, nh):
    """Area-average (box filter) downscale — better antialiasing than nearest."""
    out = bytearray([0, 0, 0, 0]) * (nw * nh)
    for yy in range(nh):
        y0 = yy * h // nh
        y1 = max(y0 + 1, (yy + 1) * h // nh)
        for xx in range(nw):
            x0 = xx * w // nw
            x1 = max(x0 + 1, (xx + 1) * w // nw)
            r = g = b = a = 0
            n = 0
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    i = sy * stride + sx * 4
                    r += raw[i]; g += raw[i+1]; b += raw[i+2]; a += raw[i+3]
                    n += 1
            o = (yy * nw + xx) * 4
            out[o] = r // n; out[o+1] = g // n; out[o+2] = b // n; out[o+3] = a // n
    return bytes(out)

def write_png(path, nw, nh, rgba):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        return c
    raw = b"".join(b"\x00" + rgba[y*nw*4:(y+1)*nw*4] for y in range(nh))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)

for size in (512, 256):
    p = f"{outdir}/icon-{size}.png"
    write_png(p, size, size, resize_area(size, size))
    print("    wrote", p)
PYEOF

echo "==> iconutil → icon.icns (macOS only; skipped on Linux/Windows)"
if command -v iconutil >/dev/null 2>&1; then
  ICONSET="$OUT/icon.iconset"
  mkdir -p "$ICONSET"
  # iconutil wants specific sizes
  for s in 16 32 128 256 512; do
    python3 - "$OUT/icon.png" "$ICONSET/icon_${s}x${s}.png" "$s" <<'PYEOF'
import sys, zlib, struct
src, dst, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
data = open(src, "rb").read(); pos, w, h, idat, ct = 8, 0, 0, b"", 0
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]; tag = data[pos+4:pos+8]; chunk = data[pos+8:pos+8+ln]
    if tag == b"IHDR": w, h = struct.unpack(">II", chunk[:8]); ct = chunk[9]
    elif tag == b"IDAT": idat += chunk
    pos += 12 + ln
raw = zlib.decompress(idat)
if ct == 6:
    stride0 = w*4 + 1
    raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
else:  # ct==2 RGB → expand to RGBA
    stride0 = w*3 + 1
    rows = [raw[y*stride0+1:(y+1)*stride0] for y in range(h)]
    out = bytearray()
    for row in rows:
        for i in range(0, len(row), 3):
            out += row[i:i+3] + b"\xff"
    raw = bytes(out)
stride = w*4
def resize_area(nw, nh):
    out = bytearray([0,0,0,0])*(nw*nh)
    for yy in range(nh):
        y0 = yy*h//nh; y1 = max(y0+1, (yy+1)*h//nh)
        for xx in range(nw):
            x0 = xx*w//nw; x1 = max(x0+1, (xx+1)*w//nw)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    i = sy*stride + sx*4
                    r += raw[i]; g += raw[i+1]; b += raw[i+2]; a += raw[i+3]; n += 1
            o = (yy*nw+xx)*4
            out[o] = r//n; out[o+1] = g//n; out[o+2] = b//n; out[o+3] = a//n
    return bytes(out)
def chunk(tag, data):
    c = struct.pack(">I", len(data))+tag+data; c += struct.pack(">I", zlib.crc32(tag+data)&0xffffffff); return c
rgba = resize_area(size, size)
raw2 = b"".join(b"\x00"+rgba[y*size*4:(y+1)*size*4] for y in range(size))
png = b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))+chunk(b"IDAT", zlib.compress(raw2,9))+chunk(b"IEND", b"")
open(dst, "wb").write(png)
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
import sys, zlib, struct
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read(); pos, w, h, idat, ct = 8, 0, 0, b"", 0
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]; tag = data[pos+4:pos+8]; chunk = data[pos+8:pos+8+ln]
    if tag == b"IHDR": w, h = struct.unpack(">II", chunk[:8]); ct = chunk[9]
    elif tag == b"IDAT": idat += chunk
    pos += 12 + ln
raw = zlib.decompress(idat)
if ct == 6:
    stride0 = w*4 + 1
    raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
else:  # ct==2 RGB → expand to RGBA
    stride0 = w*3 + 1
    rows = [raw[y*stride0+1:(y+1)*stride0] for y in range(h)]
    out = bytearray()
    for row in rows:
        for i in range(0, len(row), 3):
            out += row[i:i+3] + b"\xff"
    raw = bytes(out)
stride = w*4

def resize_area(nw, nh):
    out = bytearray([0,0,0,0])*(nw*nh)
    for yy in range(nh):
        y0 = yy*h//nh; y1 = max(y0+1, (yy+1)*h//nh)
        for xx in range(nw):
            x0 = xx*w//nw; x1 = max(x0+1, (xx+1)*w//nw)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    i = sy*stride + sx*4
                    r += raw[i]; g += raw[i+1]; b += raw[i+2]; a += raw[i+3]; n += 1
            o = (yy*nw+xx)*4
            out[o] = r//n; out[o+1] = g//n; out[o+2] = b//n; out[o+3] = a//n
    return bytes(out)

def png_bytes(nw, nh, rgba):
    def chunk(tag, data):
        c = struct.pack(">I", len(data))+tag+data; c += struct.pack(">I", zlib.crc32(tag+data)&0xffffffff); return c
    raw2 = b"".join(b"\x00"+rgba[y*nw*4:(y+1)*nw*4] for y in range(nh))
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 6, 0, 0, 0))+chunk(b"IDAT", zlib.compress(raw2,9))+chunk(b"IEND", b"")

# ICO: header + directory + PNG-embedded entries
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [(s, png_bytes(s, s, resize_area(s, s))) for s in sizes]
header = struct.pack("<HHH", 0, 1, len(imgs))
offset = 6 + 16*len(imgs)
entries = b""
for s, png in imgs:
    entries += struct.pack("<BBBBHHII", s if s<256 else 0, s if s<256 else 0, 0, 0, 1, 32, len(png), offset)
    offset += len(png)
with open(dst, "wb") as f:
    f.write(header + entries + b"".join(p for _, p in imgs))
print("    wrote", dst)
PYEOF

echo "==> assets:"
ls -la "$OUT"
