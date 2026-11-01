#!/usr/bin/env python3
"""Render the PWA PNG icons (same design as app/web/icons/icon.svg) without external libraries.

Full-bleed background so the icons work as maskable icons; content stays in the safe zone.
Usage: scripts/make_icons.py   (writes app/web/icons/icon-192.png and icon-512.png)
"""
import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEAL, WHITE = (15, 118, 110), (255, 255, 255)
DOTS = [((186, 248), 1.0), ((256, 248), 0.7), ((326, 248), 0.45)]
SS = 3  # supersampling per axis


def in_round_rect(x, y, x0, y0, x1, y1, r):
    cx, cy = min(max(x, x0 + r), x1 - r), min(max(y, y0 + r), y1 - r)
    return x0 <= x <= x1 and y0 <= y <= y1 and (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_tail(x, y):
    # Triangle (176,346) (246,346) (176,400)
    return 176 <= x and 346 <= y and (x - 176) / 70 + (y - 346) / 54 <= 1


def sample(x, y):
    if not (in_round_rect(x, y, 88, 150, 424, 346, 48) or in_tail(x, y)):
        return TEAL
    for (cx, cy), alpha in DOTS:
        if (x - cx) ** 2 + (y - cy) ** 2 <= 22 * 22:
            return tuple(round(t * alpha + w * (1 - alpha)) for t, w in zip(TEAL, WHITE))
    return WHITE


def render(size):
    scale = 512 / size
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0
        for px in range(size):
            acc = [0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = sample((px + (sx + 0.5) / SS) * scale, (py + (sy + 0.5) / SS) * scale)
                    acc = [a + b for a, b in zip(acc, c)]
            row += bytes(round(a / SS / SS) for a in acc)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


if __name__ == "__main__":
    for size in (192, 512):
        out = ROOT / "app" / "web" / "icons" / f"icon-{size}.png"
        out.write_bytes(render(size))
        print("wrote", out)
