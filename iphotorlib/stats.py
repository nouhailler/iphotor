"""Statistiques d'une photothèque, calculées localement (index + index des métadonnées).

Entrée : des enregistrements normalisés (dict) — aucune lecture de fichier source.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections import Counter
from typing import Iterable, Optional

from .metadata import RAW_EXTS, VIDEO_EXTS
from .timeline import MONTHS_FR

MONTHS_SHORT = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
ISO_BUCKETS = [(0, 100, "≤ 100"), (101, 200, "101–200"), (201, 400, "201–400"), (401, 800, "401–800"),
               (801, 1600, "801–1600"), (1601, 3200, "1601–3200"), (3201, 10 ** 9, "> 3200")]
FOCAL_BUCKETS = [(0, 20, "< 20 mm"), (20, 35, "20–34 mm"), (35, 50, "35–49 mm"), (50, 85, "50–84 mm"),
                 (85, 135, "85–134 mm"), (135, 300, "135–299 mm"), (300, 10 ** 6, "≥ 300 mm")]


def _bucket(value, buckets) -> Optional[str]:
    if value is None:
        return None
    for lo, hi, label in buckets:
        if lo <= value <= hi or (lo <= value < hi):
            return label
    return None


def compute(records: Iterable[dict]) -> dict:
    """records : dicts avec filename, media_type, date (Unix), et facultativement les champs de
    l'index des métadonnées (status, file_size, width, height, has_exif, make, model, lens, iso,
    focal, focal35, gps_lat). Renvoie un dictionnaire de statistiques."""
    n = videos = raws = analysed = with_exif = gps = no_date = 0
    sizes: list[int] = []
    ext = Counter()
    years = Counter()
    months = Counter()
    ym = Counter()
    dims = Counter()
    cams = Counter()
    lenses = Counter()
    isos = Counter()
    focals = Counter()
    for r in records:
        n += 1
        name = r.get("filename") or ""
        e = os.path.splitext(name)[1].lower()
        ext[e.lstrip(".").upper() or "(sans)"] += 1
        if (r.get("media_type") or "").lower() == "movie" or e in VIDEO_EXTS:
            videos += 1
        if e in RAW_EXTS:
            raws += 1
        ts = r.get("date")
        d = None
        if ts is not None:
            try:
                d = _dt.datetime.fromtimestamp(ts)
                if d.year < 1900 or d.year > _dt.date.today().year + 1:
                    d = None
            except (OverflowError, OSError, ValueError):
                d = None
        if d is None:
            no_date += 1
        else:
            years[d.year] += 1
            months[d.month] += 1
            ym[(d.year, d.month)] += 1
        size = r.get("file_size") or r.get("size")
        if size:
            sizes.append(int(size))
        if r.get("status") == "ok":
            analysed += 1
            if r.get("has_exif"):
                with_exif += 1
            if r.get("gps_lat") is not None:
                gps += 1
            if r.get("width") and r.get("height"):
                dims[f"{r['width']} × {r['height']}"] += 1
            cam = " ".join(x for x in (r.get("make") or "", r.get("model") or "") if x).strip()
            if cam:
                # évite « Canon Canon EOS 450D »
                mk, md = (r.get("make") or "").strip(), (r.get("model") or "").strip()
                cam = md if mk and md.lower().startswith(mk.split()[0].lower()) else cam
                cams[cam] += 1
            if r.get("lens"):
                lenses[r["lens"]] += 1
            b = _bucket(r.get("iso"), ISO_BUCKETS)
            if b:
                isos[b] += 1
            b = _bucket(r.get("focal35") or r.get("focal"), FOCAL_BUCKETS)
            if b:
                focals[b] += 1
    total_size = sum(sizes)
    return {
        "total": n, "videos": videos, "photos": n - videos, "raw": raws,
        "extensions": ext.most_common(),
        "years": sorted(years.items()),
        "months": [(MONTHS_SHORT[m - 1], months.get(m, 0)) for m in range(1, 13)],
        "year_months": sorted(ym.items()),
        "size_known": len(sizes), "size_total": total_size,
        "size_avg": (total_size / len(sizes)) if sizes else 0,
        "dimensions": dims.most_common(10),
        "cameras": cams.most_common(15), "lenses": lenses.most_common(15),
        "iso": [(lbl, isos.get(lbl, 0)) for _a, _b, lbl in ISO_BUCKETS],
        "focal": [(lbl, focals.get(lbl, 0)) for _a, _b, lbl in FOCAL_BUCKETS],
        "analysed": analysed, "with_exif": with_exif, "without_exif": analysed - with_exif,
        "gps": gps, "gps_pct": (100.0 * gps / analysed) if analysed else 0.0,
        "no_date": no_date,
    }


def format_bytes(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)
