#!/usr/bin/env python3
"""Generate the PWA icons without an image library.

iOS requires PNG for the home-screen icon (an SVG in the manifest is not
enough), so these are written as raw RGBA PNGs: a dark rounded square with a
simple ascending bar chart. Run this only to regenerate the files.
"""

from __future__ import annotations

import pathlib
import struct
import zlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "frontend/public"

BG = (13, 17, 23, 255)        # near-black, matches the app background
PANEL = (22, 27, 34, 255)
BARS = [(56, 139, 253, 255), (63, 185, 80, 255), (210, 153, 34, 255), (248, 81, 73, 255)]


def rounded(x: int, y: int, size: int, radius: int) -> bool:
    """Is (x, y) inside a rounded square of `size` with corner `radius`?"""
    for cx, cy in ((radius, radius), (size - radius, radius),
                   (radius, size - radius), (size - radius, size - radius)):
        inside_x = (x < radius) if cx == radius else (x > size - radius)
        inside_y = (y < radius) if cy == radius else (y > size - radius)
        if inside_x and inside_y:
            return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2
    return True


def build(size: int) -> bytes:
    radius = size // 5
    margin = size // 6
    plot = size - 2 * margin
    bar_w = plot // 7

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG filter type 0 for this scanline
        for x in range(size):
            if not rounded(x, y, size, radius):
                rows.extend((0, 0, 0, 0))
                continue

            pixel = BG
            if margin <= x < size - margin and margin <= y < size - margin:
                pixel = PANEL
                for index in range(4):
                    left = margin + index * (bar_w + bar_w // 2)
                    height = int(plot * (0.3 + 0.175 * index))
                    if left <= x < left + bar_w and y >= size - margin - height:
                        pixel = BARS[index]
                        break
            rows.extend(pixel)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        path = OUT / f"icon-{size}.png"
        path.write_bytes(build(size))
        print(f"wrote {path} ({path.stat().st_size} bytes)")
