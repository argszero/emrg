"""Filter-aware PNG decode regression tests (packaging/pngutil.py).

Lesson 2026-08-12 v0.2.29 Build Release: gen-assets.sh derived icons from
icon.png rendered by rsvg-convert / Chrome headless. Both emit PNG scanlines
with adaptive filters (Sub/Up/Average/Paeth — libpng default). The old inline
decoders stripped the leading filter byte WITHOUT reversing the filter, so:
  - the transparency check read deltas instead of alpha → a fully opaque icon
    falsely reported "99.2% transparent" (Linux/macOS CI failure), and
  - the area-average resize produced garbage color artifacts from filtered
    input (silently wrong icon-512/256/icns/ico in shipped builds).

These tests pin the filter reversal: decode must match the source pixels for
every PNG filter type, and the opacity check must keep discriminating real
transparent renders (positive/negative states).
"""

import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))
import pngutil  # noqa: E402


def _make_png(w, h, rgba_rows, filter_type):
    """Encode RGBA rows (each a bytes of w*4 pixels) with a fixed filter type."""
    bpp = 4
    stride = w * bpp

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    out = bytearray()
    prev = bytearray(stride)
    for row in rgba_rows:
        line = bytearray(row)
        if filter_type == 0:
            out += b"\x00" + line
        else:
            filt = bytearray([filter_type])
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                if filter_type == 1:  # Sub
                    pr = left
                elif filter_type == 2:  # Up
                    pr = prev[i]
                elif filter_type == 3:  # Average
                    pr = (left + prev[i]) >> 1
                else:  # Paeth
                    b = prev[i]
                    c = prev[i - bpp] if i >= bpp else 0
                    pa, pb, pc = abs(b - c), abs(left - c), abs(left + b - 2 * c)
                    pr = left if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                filt.append((line[i] - pr) & 0xFF)
            out += filt
        prev = line

    raw = bytes(out)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return png


def _solid_rgba(w, h, color):
    """h rows of w RGBA pixels, all `color`."""
    row = color * w
    return [row] * h


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_decode_reverses_each_filter_type(tmp_path, filter_type):
    """Every PNG filter type decodes back to the original pixels."""
    w = h = 16
    color = bytes([17, 28, 22, 255])
    p = tmp_path / f"f{filter_type}.png"
    p.write_bytes(_make_png(w, h, _solid_rgba(w, h, color), filter_type))

    dw, dh, bpp, px = pngutil.read_png(str(p))
    assert (dw, dh, bpp) == (w, h, 4)
    assert px == color * (w * h), f"filter {filter_type} decode mismatch"


def test_rgb_decode_expands_alpha(tmp_path):
    """Chrome emits RGB (colortype 2, 3bpp) → read_png returns 3bpp pixels."""
    w = h = 8
    # Minimal RGB PNG (colortype 2) — build manually via write path then re-read
    # (pngutil writes RGBA; synthesize RGB with a tiny inline encoder).
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    row = b"\x00" + bytes([10, 20, 30]) * w  # filter 0 + RGB pixels
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * h, 9))
        + chunk(b"IEND", b"")
    )
    p = tmp_path / "rgb.png"
    p.write_bytes(png)

    dw, dh, bpp, px = pngutil.read_png(str(p))
    assert (dw, dh, bpp) == (w, h, 3)
    rgba = pngutil.to_rgba(dw, dh, bpp, px)
    assert rgba == bytes([10, 20, 30, 255]) * (w * h)


def test_opaque_ratio_true_positive(tmp_path):
    """Fully opaque filtered RGBA must read as 100% opaque (regression: the old
    naive decoder reported 0.8% on this input)."""
    w = h = 64
    color = bytes([17, 28, 22, 255])
    p = tmp_path / "opaque.png"
    p.write_bytes(_make_png(w, h, _solid_rgba(w, h, color), 3))  # Average filter

    dw, dh, bpp, px = pngutil.read_png(str(p))
    assert pngutil.opaque_ratio(dw, dh, bpp, px) == 1.0


def test_opaque_ratio_true_negative(tmp_path):
    """A genuinely transparent render must still be caught (<10% opaque)."""
    w = h = 64
    color = bytes([0, 0, 0, 0])
    p = tmp_path / "transparent.png"
    p.write_bytes(_make_png(w, h, _solid_rgba(w, h, color), 1))  # Sub filter

    dw, dh, bpp, px = pngutil.read_png(str(p))
    assert pngutil.opaque_ratio(dw, dh, bpp, px) == 0.0
    assert pngutil.opaque_ratio(dw, dh, bpp, px) < 0.10


def test_resize_area_preserves_colors(tmp_path):
    """Area-average of a solid color stays that color (old bug: garbage hues)."""
    w = h = 64
    color = bytes([17, 28, 22, 255])
    p = tmp_path / "src.png"
    p.write_bytes(_make_png(w, h, _solid_rgba(w, h, color), 4))  # Paeth filter

    dw, dh, bpp, px = pngutil.read_png(str(p))
    rgba = pngutil.to_rgba(dw, dh, bpp, px)
    small = pngutil.resize_area(rgba, dw, dh, 16, 16)
    assert small == color * (16 * 16)


def test_write_png_roundtrip_filter0(tmp_path):
    """write_png emits filter-0 rows that read_png decodes losslessly."""
    w = h = 8
    rgba = bytes([5, 6, 7, 255]) * (w * h)
    p = tmp_path / "out.png"
    pngutil.write_png(str(p), w, h, rgba)

    dw, dh, bpp, px = pngutil.read_png(str(p))
    assert (dw, dh, bpp) == (w, h, 4)
    assert px == rgba
