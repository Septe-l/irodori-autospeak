#!/usr/bin/env python3
"""Generate the PWA icons.

Written against the standard library only — the project has no image
dependency and does not need one at runtime, since the icons are generated
once and committed. Run this again after changing the palette or the mark:

    ./.venv/bin/python tools/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"

BG = (0x14, 0x16, 0x1A)       # --bg
ACCENT = (0x6F, 0xC3, 0xFF)   # --accent
DIM = (0x2B, 0x4D, 0x63)      # --accent-dim

# Bar heights as a fraction of the icon, drawn as a speech waveform. The mark
# stays inside the middle 60% so it survives a maskable crop.
BARS = (0.34, 0.62, 0.90, 0.62, 0.34)


def rounded_rect(px, w, h, x0, y0, x1, y1, radius, color):
    r2 = radius * radius
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            dx = dy = 0
            if x < x0 + radius:
                dx = x0 + radius - x
            elif x >= x1 - radius:
                dx = x - (x1 - radius - 1)
            if y < y0 + radius:
                dy = y0 + radius - y
            elif y >= y1 - radius:
                dy = y - (y1 - radius - 1)
            if dx and dy and dx * dx + dy * dy > r2:
                continue
            px[y * w + x] = color


def render(size: int, *, squircle: bool) -> bytes:
    w = h = size
    # Maskable icons are cropped by the launcher, so the background must bleed
    # to the edges; the standalone icon gets its own rounded corners instead.
    px = [(0, 0, 0, 0)] * (w * h) if squircle else [BG + (255,)] * (w * h)

    if squircle:
        rounded_rect(px, w, h, 0, 0, w, h, int(size * 0.22), BG + (255,))

    bar_w = max(2, int(size * 0.075))
    gap = max(2, int(size * 0.045))
    total = len(BARS) * bar_w + (len(BARS) - 1) * gap
    x = (w - total) // 2
    radius = bar_w // 2

    for i, frac in enumerate(BARS):
        bar_h = int(size * 0.46 * frac)
        y0 = (h - bar_h) // 2
        colour = ACCENT if i % 2 == 0 else DIM
        rounded_rect(px, w, h, x, y0, x + bar_w, y0 + bar_h, radius, colour + (255,))
        x += bar_w + gap

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0
        for p in px[y * w:(y + 1) * w]:
            raw.extend(p)
    return png(w, h, bytes(raw))


def png(w: int, h: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        # iOS applies its own mask, so this one keeps square edges and a full bleed.
        ("apple-touch-icon.png", 180, False),
        ("icon-192-rounded.png", 192, True),
    ]
    for name, size, squircle in targets:
        data = render(size, squircle=squircle)
        (OUT / name).write_bytes(data)
        print(f"  {name:26} {size}x{size}  {len(data) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
