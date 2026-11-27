"""Outils communs aux tests : images synthétiques avec EXIF, empreintes de lecture seule."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw  # noqa: E402


def scene(w: int = 640, h: int = 420, seed: int = 0) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    c1 = tuple(rnd.randint(20, 235) for _ in range(3))
    c2 = tuple(rnd.randint(20, 235) for _ in range(3))
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    for _ in range(4):
        x, y, r = rnd.randint(0, w), rnd.randint(0, h), rnd.randint(h // 10, h // 3)
        d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(rnd.randint(0, 255) for _ in range(3)))
    pts = [(0, h)] + [(x, h * 0.6 - h * 0.15 * math.sin(x / w * 6 + seed)) for x in range(0, w + 10, 10)] + [(w, h)]
    d.polygon(pts, fill=tuple(rnd.randint(0, 120) for _ in range(3)))
    return img


def make_exif(make="Canon", model="Canon EOS 450D", when=dt.datetime(2015, 6, 1, 12, 0), iso=200,
              focal=50.0, aperture=2.8, exposure=(1, 125), gps=None, lens="EF50mm f/1.8"):
    ex = Image.Exif()
    ex[0x010F] = make
    ex[0x0110] = model
    ex[0x0132] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub = ex.get_ifd(0x8769)
    sub[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x8827] = iso
    sub[0x920A] = (int(focal * 10), 10)
    sub[0x829D] = (int(aperture * 10), 10)
    sub[0x829A] = exposure
    sub[0xA434] = lens
    if gps:
        lat, lon = gps
        g = ex.get_ifd(0x8825)

        def dms(v):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            s = round(((v - d) * 60 - m) * 60, 2)
            return (float(d), float(m), float(s))
        g[1] = "N" if lat >= 0 else "S"
        g[2] = dms(lat)
        g[3] = "E" if lon >= 0 else "W"
        g[4] = dms(lon)
    return ex


def write_jpeg(path: str, seed: int = 0, size=(640, 420), **exif_kw) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exif = exif_kw.pop("exif", None)
    if exif is None and exif_kw.get("no_exif") is not True:
        exif = make_exif(**{k: v for k, v in exif_kw.items() if k != "no_exif"})
    img = scene(*size, seed=seed)
    if exif is not None:
        img.save(path, "JPEG", quality=88, exif=exif)
    else:
        img.save(path, "JPEG", quality=88)
    return path


def snapshot(*roots: str) -> dict:
    """Empreinte complète (SHA-256, taille, date, liste) de toutes les sources."""
    out = {}
    for root in roots:
        for d, dirs, files in os.walk(root):
            out[d] = ("dir", sorted(dirs), sorted(files))
            for f in files:
                p = os.path.join(d, f)
                st = os.stat(p)
                with open(p, "rb") as fh:
                    out[p] = (hashlib.sha256(fh.read()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def diff(before: dict, after: dict) -> list:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))
