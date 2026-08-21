#!/usr/bin/env python3
"""Filter-aware PNG helpers for packaging/gen-assets.sh.

gen-assets.sh renders icon.svg → icon.png with rsvg-convert / Chrome headless,
then derives icon-512/256.png, icon.icns and icon.ico from it. Both renderers
emit PNG scanlines with adaptive filters (Sub/Up/Average/Paeth — libpng
default). Any consumer that strips the leading filter byte WITHOUT reversing
the filter reads garbage pixels (2026-08-12 v0.2.29 Build Release: the
transparency check falsely reported icon.png "99.2% transparent" and the
area-average resize produced garbage color artifacts from filtered input).

This module provides the single correct decode path (reverse filters, expand
RGB→RGBA) plus the resize/write helpers so every derivation site shares one
implementation. Stdlib-only — runs on the CI runners and the host.
"""

import struct
import zlib


def read_png(path):
    """Decode a PNG to (w, h, bpp, pixels) with scanline filters reversed.

    bpp is 4 for RGBA (colortype 6) or 3 for RGB (colortype 2); other
    colortypes raise ValueError. pixels is the raw decoded rows (filter bytes
    removed, filters applied) — RGB rows are NOT expanded here so callers can
    keep the compact form; use to_rgba() when uniform 4bpp is needed.
    """
    with open(path, "rb") as f:
        data = f.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: not a PNG"
    pos, w, h, idat, ct = 8, 0, 0, b"", 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
            ct = chunk[9]
        elif tag == b"IDAT":
            idat += chunk
        pos += 12 + ln
    bpp = {2: 3, 6: 4}.get(ct)
    if bpp is None:
        raise ValueError(f"{path}: unsupported PNG colortype {ct}")
    raw = zlib.decompress(idat)
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        # ft == 0 (None): unchanged
        out += line
        prev = line
    return w, h, bpp, bytes(out)


def to_rgba(w, h, bpp, pixels):
    """Expand RGB (3bpp) rows to RGBA (4bpp, alpha=255); RGBA passes through."""
    if bpp == 4:
        return pixels
    out = bytearray()
    for i in range(0, len(pixels), 3):
        out += pixels[i:i + 3] + b"\xFF"
    return bytes(out)


def opaque_ratio(w, h, bpp, pixels, step=8):
    """Fraction of sampled pixels with alpha > 0 (0.0–1.0). RGBA only."""
    if bpp != 4:
        return 1.0  # RGB PNG has no alpha channel — nothing to check
    total = opaque = 0
    for y in range(0, h, step):
        for x in range(0, w, step):
            a = pixels[y * w * 4 + x * 4 + 3]
            total += 1
            if a > 0:
                opaque += 1
    return opaque / total


def resize_area(rgba, w, h, nw, nh):
    """Area-average (box filter) downscale of RGBA pixels."""
    stride = w * 4
    out = bytearray([0, 0, 0, 0]) * (nw * nh)
    for yy in range(nh):
        y0 = yy * h // nh
        y1 = max(y0 + 1, (yy + 1) * h // nh)
        for xx in range(nw):
            x0 = xx * w // nw
            x1 = max(x0 + 1, (xx + 1) * w // nw)
            r = g = b = a = n = 0
            for sy in range(y0, y1):
                for sx in range(x0, x1):
                    i = sy * stride + sx * 4
                    r += rgba[i]
                    g += rgba[i + 1]
                    b += rgba[i + 2]
                    a += rgba[i + 3]
                    n += 1
            o = (yy * nw + xx) * 4
            out[o] = r // n
            out[o + 1] = g // n
            out[o + 2] = b // n
            out[o + 3] = a // n
    return bytes(out)


def write_png(path, nw, nh, rgba):
    """Write RGBA pixels as a PNG (filter 0 rows, colortype 6)."""
    with open(path, "wb") as f:
        f.write(write_png_bytes(nw, nh, rgba))


def write_png_bytes(nw, nh, rgba):
    """Return RGBA pixels as PNG bytes (filter 0 rows, colortype 6)."""

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    raw = b"".join(b"\x00" + rgba[y * nw * 4:(y + 1) * nw * 4] for y in range(nh))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", nw, nh, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
