#!/usr/bin/env bash
# EMRG Phase 4 — generate packaging assets (icons).
#
# Pure-stdlib approach (no PIL/ImageMagick dependency):
#   1. Python stdlib draws a 1024x1024 PNG (EMRG glyph: dark bg + green "E" + circuit node)
#   2. iconutil (macOS) → .icns
#   3. Python stdlib → multi-size .ico (win)
#   4. 512/256 PNG copies (linux)
#
# Output in packaging/assets/:
#   icon.png (1024)  icon-512.png  icon-256.png  icon.icns  icon.ico
#
# Run from repo root: bash packaging/gen-assets.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/packaging/assets"
mkdir -p "$OUT"

echo "==> generating 1024x1024 icon.png (pure stdlib)"
python3 - "$OUT/icon.png" <<'PYEOF'
import sys, struct, zlib, math

path = sys.argv[1]
S = 1024
# RGBA buffer (transparent bg)
buf = bytearray([0, 0, 0, 0]) * (S * S)

def put(x, y, r, g, b, a=255):
    if 0 <= x < S and 0 <= y < S:
        i = (y * S + x) * 4
        buf[i] = r; buf[i+1] = g; buf[i+2] = b; buf[i+3] = a

def fill_circle(cx, cy, rad, r, g, b, a=255):
    # bounding box fill
    x0, x1 = max(0, int(cx-rad)), min(S, int(cx+rad)+1)
    y0, y1 = max(0, int(cy-rad)), min(S, int(cy+rad)+1)
    r2 = rad*rad
    for yy in range(y0, y1):
        for xx in range(x0, x1):
            dx, dy = xx-cx, yy-cy
            if dx*dx + dy*dy <= r2:
                put(xx, yy, r, g, b, a)

def fill_rect(x0, y0, x1, y1, r, g, b, a=255):
    for yy in range(max(0,y0), min(S,y1)):
        for xx in range(max(0,x0), min(S,x1)):
            put(xx, yy, r, g, b, a)

def fill_round_rect(x0, y0, x1, y1, rad, r, g, b, a=255):
    fill_rect(x0+rad, y0, x1-rad, y1, r, g, b, a)
    fill_rect(x0, y0+rad, x1, y1-rad, r, g, b, a)
    fill_circle(x0+rad, y0+rad, rad, r, g, b, a)
    fill_circle(x1-rad, y0+rad, rad, r, g, b, a)
    fill_circle(x0+rad, y1-rad, rad, r, g, b, a)
    fill_circle(x1-rad, y1-rad, rad, r, g, b, a)

# background: rounded square (EMRG brand: dark navy #0f172a)
fill_round_rect(64, 64, S-64, S-64, 180, 0x0f, 0x17, 0x2a)
# subtle outer ring
for rad in range(460, 480):
    fill_circle(S//2, S//2, rad, 0x1e, 0x29, 0x3b, 255)

# circuit: ring + node (self-evolving motif)
fill_circle(S//2, S//2, 300, 0x10, 0xb9, 0x81, 255)      # green ring body
fill_circle(S//2, S//2, 250, 0x0f, 0x17, 0x2a, 255)      # punch hole → ring
# 4 orbit nodes
for ang in (0, 90, 180, 270):
    ox = S//2 + int(380 * math.cos(math.radians(ang)))
    oy = S//2 + int(380 * math.sin(math.radians(ang)))
    fill_circle(ox, oy, 40, 0x10, 0xb9, 0x81, 255)

# "E" glyph (three bars + spine) — simple block letter
bar = 70
spine_x0, spine_x1 = S//2 - 200, S//2 - 130
top_y, mid_y, bot_y = S//2 - 200, S//2 - 30, S//2 + 140
fill_rect(spine_x0, top_y, spine_x1, bot_y+bar, 0xf8, 0xfa, 0xfc)       # vertical spine
fill_rect(spine_x0, top_y, S//2 + 200, top_y+bar, 0xf8, 0xfa, 0xfc)     # top bar
fill_rect(spine_x0, mid_y, S//2 + 140, mid_y+bar, 0xf8, 0xfa, 0xfc)     # middle bar
fill_rect(spine_x0, bot_y, S//2 + 200, bot_y+bar, 0xf8, 0xfa, 0xfc)     # bottom bar

def write_png(path, w, h, rgba):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        return c
    raw = b"".join(b"\x00" + bytes(rgba[y*w*4:(y+1)*w*4]) for y in range(h))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)

write_png(path, S, S, buf)
print("    wrote", path)
PYEOF

echo "==> resizing to 512/256 (stdlib scale-down)"
python3 - "$OUT/icon.png" "$OUT" <<'PYEOF'
import sys, zlib, struct

src, outdir = sys.argv[1], sys.argv[2]

def read_png(path):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, w, h, idat = 8, 0, 0, b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos+4])[0]
        tag = data[pos+4:pos+8]
        chunk = data[pos+8:pos+8+ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    raw = zlib.decompress(idat)
    # PNG scanlines each have a leading filter byte (0 for None); strip them
    stride0 = w * 4 + 1
    raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
    return w, h, raw

w, h, raw = read_png(src)
stride = w * 4

def sample(x, y):
    i = y * stride + x * 4
    return raw[i], raw[i+1], raw[i+2], raw[i+3]

def resize(nw, nh):
    out = bytearray([0, 0, 0, 0]) * (nw * nh)
    for yy in range(nh):
        sy = min(int(yy * h / nh), h-1)
        for xx in range(nw):
            sx = min(int(xx * w / nw), w-1)
            i = sy * stride + sx * 4
            r, g, b, a = raw[i], raw[i+1], raw[i+2], raw[i+3]
            o = (yy * nw + xx) * 4
            out[o] = r; out[o+1] = g; out[o+2] = b; out[o+3] = a
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
    write_png(p, size, size, resize(size, size))
    print("    wrote", p)
PYEOF

echo "==> iconutil → icon.icns (macOS)"
ICONSET="$OUT/icon.iconset"
mkdir -p "$ICONSET"
# iconutil wants specific sizes
for s in 16 32 128 256 512; do
  d=$((s*2))
  python3 - "$OUT/icon.png" "$ICONSET/icon_${s}x${s}.png" "$s" <<'PYEOF'
import sys, zlib, struct
src, dst, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
# reuse resize from above by re-reading
data = open(src, "rb").read(); pos, w, h, idat = 8, 0, 0, b""
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]; tag = data[pos+4:pos+8]; chunk = data[pos+8:pos+8+ln]
    if tag == b"IHDR": w, h = struct.unpack(">II", chunk[:8])
    elif tag == b"IDAT": idat += chunk
    pos += 12 + ln
raw = zlib.decompress(idat)
stride0 = w*4 + 1
raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
stride = w*4
out = bytearray([0,0,0,0])*(size*size)
for yy in range(size):
    sy = yy*h//size
    for xx in range(size):
        sx = min(xx*w//size, w-1)
        i = sy*stride + sx*4
        o = (yy*size+xx)*4
        out[o]=raw[i]; out[o+1]=raw[i+1]; out[o+2]=raw[i+2]; out[o+3]=raw[i+3]
def chunk(tag, data):
    c = struct.pack(">I", len(data))+tag+data; c += struct.pack(">I", zlib.crc32(tag+data)&0xffffffff); return c
rgba = bytes(out)
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

echo "==> icon.ico (multi-size, win)"
python3 - "$OUT/icon.png" "$OUT/icon.ico" <<'PYEOF'
import sys, zlib, struct
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read(); pos, w, h, idat = 8, 0, 0, b""
while pos < len(data):
    ln = struct.unpack(">I", data[pos:pos+4])[0]; tag = data[pos+4:pos+8]; chunk = data[pos+8:pos+8+ln]
    if tag == b"IHDR": w, h = struct.unpack(">II", chunk[:8])
    elif tag == b"IDAT": idat += chunk
    pos += 12 + ln
raw = zlib.decompress(idat)
stride0 = w*4 + 1
raw = b"".join(raw[y*stride0+1:(y+1)*stride0] for y in range(h))
stride = w*4

def resize(nw, nh):
    out = bytearray([0,0,0,0])*(nw*nh)
    for yy in range(nh):
        sy = min(yy*h//nh, h-1)
        for xx in range(nw):
            sx = min(xx*w//nw, w-1)
            i = sy*stride + sx*4
            o = (yy*nw+xx)*4
            out[o]=raw[i]; out[o+1]=raw[i+1]; out[o+2]=raw[i+2]; out[o+3]=raw[i+3]
    return bytes(out)

def png_bytes(nw, nh, rgba):
    def chunk(tag, data):
        c = struct.pack(">I", len(data))+tag+data; c += struct.pack(">I", zlib.crc32(tag+data)&0xffffffff); return c
    raw2 = b"".join(b"\x00"+rgba[y*nw*4:(y+1)*nw*4] for y in range(nh))
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 6, 0, 0, 0))+chunk(b"IDAT", zlib.compress(raw2,9))+chunk(b"IEND", b"")

# ICO: header + directory + PNG-embedded entries
sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [(s, png_bytes(s, s, resize(s, s))) for s in sizes]
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
