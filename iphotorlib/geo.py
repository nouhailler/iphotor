"""Exploration géographique hors ligne.

- Regroupement des photos géolocalisées par grille (≈ 100 km, 10 km, 1 km).
- Noms de lieux uniquement s'ils proviennent des bases Apple (aucun géocodage inventé).
- Projection pour une carte locale (équirectangulaire, sans fond de carte téléchargé).
- Lien OpenStreetMap construit seulement à la demande explicite de l'utilisateur.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

LEVELS = [("zone", 1.0, "Zone (≈ 100 km)"), ("secteur", 0.1, "Secteur (≈ 10 km)"), ("lieu", 0.01, "Lieu (≈ 1 km)")]


@dataclass
class Point:
    key: str
    photo_id: Optional[int]
    lat: float
    lon: float
    label: str = ""
    place: str = ""          # nom de lieu Apple, si connu


@dataclass
class Cluster:
    level: str
    lat: float               # centre (moyenne)
    lon: float
    points: list = field(default_factory=list)
    places: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.places:
            best = max(self.places.items(), key=lambda kv: kv[1])[0]
            return best
        return format_coords(self.lat, self.lon)


def format_coords(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "O"
    return f"{abs(lat):.4f}° {ns}, {abs(lon):.4f}° {ew}".replace(".", ",")


def clusters(points: Iterable[Point], level: str = "secteur") -> list[Cluster]:
    step = dict((n, s) for n, s, _ in LEVELS)[level]
    cells: dict[tuple[int, int], list[Point]] = {}
    for p in points:
        cells.setdefault((math.floor(p.lat / step), math.floor(p.lon / step)), []).append(p)
    out = []
    for pts in cells.values():
        c = Cluster(level, sum(p.lat for p in pts) / len(pts), sum(p.lon for p in pts) / len(pts), pts)
        for p in pts:
            if p.place:
                c.places[p.place] = c.places.get(p.place, 0) + 1
        out.append(c)
    out.sort(key=lambda c: -len(c.points))
    return out


def bounds(points: Iterable[Point], margin: float = 0.08) -> tuple[float, float, float, float]:
    pts = list(points)
    if not pts:
        return -60.0, 75.0, -180.0, 180.0
    lat0, lat1 = min(p.lat for p in pts), max(p.lat for p in pts)
    lon0, lon1 = min(p.lon for p in pts), max(p.lon for p in pts)
    dlat = max(lat1 - lat0, 0.02)
    dlon = max(lon1 - lon0, 0.02)
    return (max(-89.0, lat0 - dlat * margin), min(89.0, lat1 + dlat * margin),
            max(-180.0, lon0 - dlon * margin), min(180.0, lon1 + dlon * margin))


def project(lat: float, lon: float, box: tuple[float, float, float, float], w: int, h: int,
            pad: int = 30) -> tuple[float, float]:
    """Coordonnées → pixels (équirectangulaire corrigée par cos(latitude moyenne))."""
    lat0, lat1, lon0, lon1 = box
    k = math.cos(math.radians((lat0 + lat1) / 2)) or 1e-6
    sx = (w - 2 * pad) / max((lon1 - lon0) * k, 1e-9)
    sy = (h - 2 * pad) / max(lat1 - lat0, 1e-9)
    s = min(sx, sy)
    cx, cy = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    return w / 2 + (lon - cx) * k * s, h / 2 - (lat - cy) * s


def unproject(x: float, y: float, box: tuple[float, float, float, float], w: int, h: int,
              pad: int = 30) -> tuple[float, float]:
    lat0, lat1, lon0, lon1 = box
    k = math.cos(math.radians((lat0 + lat1) / 2)) or 1e-6
    s = min((w - 2 * pad) / max((lon1 - lon0) * k, 1e-9), (h - 2 * pad) / max(lat1 - lat0, 1e-9))
    cx, cy = (lon0 + lon1) / 2, (lat0 + lat1) / 2
    return cy - (y - h / 2) / s, cx + (x - w / 2) / (k * s)


def grid_step(span: float) -> float:
    for step in (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 20, 30, 45):
        if span / step <= 8:
            return step
    return 60


def osm_url(lat: float, lon: float, zoom: int = 14) -> str:
    """URL OpenStreetMap — à n'ouvrir QUE sur action explicite (envoie la position au site)."""
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map={zoom}/{lat:.6f}/{lon:.6f}"
