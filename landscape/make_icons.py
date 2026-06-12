#!/usr/bin/env python3
"""Generate PWA icons for the Competitive Landscape dashboard.

Pure-stdlib PNG writer (zlib + struct) — Pillow is not installed in this
environment. Draws a simple "lens" motif: a filled --accent circle with a
smaller --bg inner circle punched out, on a solid --bg field. Colors match
the page CSS vars at the top of HTML_TEMPLATE in build.py. No text — the
ring reads cleanly at home-screen sizes.

Usage: python3 landscape/make_icons.py
Emits: landscape/icons/icon-192.png, landscape/icons/icon-512.png
"""
import os
import struct
import zlib

BG = (0x0B, 0x10, 0x20)      # --bg
ACCENT = (0x29, 0xB5, 0xE8)  # --accent
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path, size, rows):
    """rows: list of `size` bytearrays, each 3*size RGB bytes."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)  # filter 0 per scanline
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _mix(a, b, t):
    return bytes(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def lens_rows(size):
    """Centered accent ring: disc r=0.34*size minus inner disc r=0.18*size,
    anti-aliased with a 1px ramp on per-pixel distance from center."""
    c = (size - 1) / 2.0
    r_outer = size * 0.34
    r_inner = size * 0.18
    rows = []
    for y in range(size):
        row = bytearray()
        dy2 = (y - c) ** 2
        for x in range(size):
            d = ((x - c) ** 2 + dy2) ** 0.5
            cov = (max(0.0, min(1.0, r_outer - d + 0.5))
                   * max(0.0, min(1.0, d - r_inner + 0.5)))
            row += _mix(BG, ACCENT, cov)
        rows.append(row)
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (192, 512):
        path = os.path.join(OUT_DIR, "icon-%d.png" % size)
        write_png(path, size, lens_rows(size))
        print("Wrote %s (%dx%d)" % (path, size, size))


if __name__ == "__main__":
    main()
