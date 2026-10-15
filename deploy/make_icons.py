"""Renders the PWA icons (numpy only). Usage: python3 make_icons.py OUT_DIR"""
import struct
import sys
import zlib
from pathlib import Path

import numpy as np


def png(path, rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    Path(path).write_bytes(data)


def sd_round_rect(x, y, cx, cy, hw, hh, r):
    qx, qy = np.abs(x - cx) - hw + r, np.abs(y - cy) - hh + r
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def sd_triangle(x, y, pts):
    """Signed distance to a convex polygon (negative inside)."""
    d = np.full(x.shape, np.inf)
    crosses = []
    n = len(pts)
    for i in range(n):
        (ax, ay), (bx, by) = pts[i], pts[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        wx, wy = x - ax, y - ay
        t = np.clip((wx * ex + wy * ey) / (ex * ex + ey * ey), 0, 1)
        d = np.minimum(d, (wx - ex * t) ** 2 + (wy - ey * t) ** 2)
        crosses.append(ex * wy - ey * wx)
    c = np.stack(crosses)
    inside = np.all(c > 0, axis=0) | np.all(c < 0, axis=0)
    return np.sqrt(d) * np.where(inside, -1, 1)


def render(size, full_bleed=True, scale=1.0):
    ss = 3
    n = size * ss
    y, x = np.mgrid[0:n, 0:n].astype(np.float64)
    x, y = (x + .5) / n, (y + .5) / n          # 0..1
    aa = 1.5 / n
    img = np.zeros((n, n, 4))
    # background: deep blue-black diagonal gradient
    g = np.clip((x + y) / 2, 0, 1)[..., None]
    bg = (1 - g) * np.array([0.09, 0.12, 0.20]) + g * np.array([0.03, 0.04, 0.07])
    if full_bleed:
        mask = np.ones((n, n))
    else:
        mask = np.clip(.5 - sd_round_rect(x, y, .5, .5, .5, .5, .22) / aa, 0, 1)
    img[..., :3] = bg
    img[..., 3] = mask
    c = .5
    k = scale
    # screen: rounded rect with gradient fill
    d_screen = sd_round_rect(x, y, c, c + .03 * k, .30 * k, .21 * k, .06 * k)
    a_screen = np.clip(.5 - d_screen / aa, 0, 1)[..., None]
    t = np.clip((x - (c - .3 * k)) / (.6 * k), 0, 1)[..., None]
    screen = (1 - t) * np.array([0.43, 0.66, 1.0]) + t * np.array([0.70, 0.55, 1.0])
    img[..., :3] = img[..., :3] * (1 - a_screen) + screen * a_screen
    # antennae
    for (x0, y0, x1, y1) in ((c - .10 * k, c - .30 * k, c, c - .19 * k), (c + .10 * k, c - .30 * k, c, c - .19 * k)):
        ex, ey = x1 - x0, y1 - y0
        tt = np.clip(((x - x0) * ex + (y - y0) * ey) / (ex * ex + ey * ey), 0, 1)
        dd = np.hypot(x - x0 - ex * tt, y - y0 - ey * tt) - .018 * k
        a = np.clip(.5 - dd / aa, 0, 1)[..., None]
        img[..., :3] = img[..., :3] * (1 - a) + np.array([0.78, 0.84, 0.98]) * a
    # play triangle
    pts = [(c - .075 * k, c - .085 * k), (c + .105 * k, c + .03 * k), (c - .075 * k, c + .145 * k)]
    d_tri = sd_triangle(x, y, pts) - .012 * k
    a_tri = np.clip(.5 - d_tri / aa, 0, 1)[..., None]
    img[..., :3] = img[..., :3] * (1 - a_tri) + np.array([1.0, 1.0, 1.0]) * a_tri
    out = img.reshape(size, ss, size, ss, 4).mean(axis=(1, 3))
    return (np.clip(out, 0, 1) * 255 + .5).astype(np.uint8)


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
png(out / "icon-512.png", render(512, full_bleed=False))
png(out / "icon-192.png", render(192, full_bleed=False))
png(out / "apple-touch-icon.png", render(180, full_bleed=True))
png(out / "icon-maskable-512.png", render(512, full_bleed=True, scale=.78))
print("ok")
