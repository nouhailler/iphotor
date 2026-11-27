"""Détection des doublons exacts (SHA-256) et des quasi-doublons (empreinte perceptuelle).

Lecture seule : les fichiers ne sont que lus. AUCUNE suppression n'est jamais proposée ;
l'interface peut seulement copier les versions retenues vers un dossier choisi.

Empreinte perceptuelle : pHash (DCT 32×32 → coefficients basse fréquence 8×8), en Python pur
avec Pillow (pas de numpy). Regroupement sans comparer toutes les paires (voir find_groups).
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

IDENTICAL, NEAR, SIMILAR = "identique", "quasi-identique", "proche"
NEAR_MAX = 6          # ≤ 6 bits sur 64 : même image redimensionnée / recompressée
SIMILAR_MAX = 12      # ≤ 12 : visuellement proches (même série possible) — jamais « doublon »

_N = 32
_COS = [[math.cos((2 * x + 1) * u * math.pi / (2 * _N)) for x in range(_N)] for u in range(8)]


def sha256_file(path: str, chunk: int = 1 << 20, cancel: Optional[threading.Event] = None) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:          # lecture seule
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def phash_image(img) -> str:
    """pHash 64 bits (hexadécimal) d'une image PIL."""
    g = ImageOps.exif_transpose(img) if hasattr(ImageOps, "exif_transpose") else img
    g = g.convert("L").resize((_N, _N), Image.BILINEAR)
    px = list(g.getdata())
    rows = [px[i * _N:(i + 1) * _N] for i in range(_N)]
    # DCT séparable, limitée aux 8 premières fréquences dans chaque direction
    tmp = [[sum(r[x] * _COS[u][x] for x in range(_N)) for u in range(8)] for r in rows]
    coef = [[sum(tmp[y][u] * _COS[v][y] for y in range(_N)) for u in range(8)] for v in range(8)]
    flat = [coef[v][u] for v in range(8) for u in range(8)]
    ac = flat[1:]
    med = sorted(ac)[len(ac) // 2]
    bits = 0
    for c in flat:
        bits = (bits << 1) | (1 if c > med else 0)
    return f"{bits:016x}"


def phash_file(path: str) -> Optional[str]:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            img.draft("RGB", (256, 256))      # décodage JPEG réduit : rapide
            return phash_image(img)
    except Exception:
        return None


def distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


@dataclass
class Entry:
    key: str
    path: str
    size: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    date: Optional[float] = None
    sha256: Optional[str] = None
    phash: Optional[str] = None
    label: str = ""


@dataclass
class Group:
    level: str
    entries: list
    distance: int = 0           # distance perceptuelle maximale dans le groupe

    @property
    def similarity(self) -> str:
        if self.level == IDENTICAL:
            return "100 % (contenu identique)"
        return f"{round(100 * (64 - self.distance) / 64)} % ({self.distance} bit{'s' if self.distance > 1 else ''} d'écart)"

    def to_json(self) -> dict:
        return {"level": self.level, "distance": self.distance,
                "entries": [e.__dict__ for e in self.entries]}

    @classmethod
    def from_json(cls, d: dict) -> "Group":
        return cls(d["level"], [Entry(**e) for e in d["entries"]], d.get("distance", 0))


@dataclass
class Report:
    groups: list = field(default_factory=list)
    analysed: int = 0
    unreadable: int = 0

    def counts(self) -> dict:
        out = {IDENTICAL: 0, NEAR: 0, SIMILAR: 0}
        for g in self.groups:
            out[g.level] += 1
        return out


SERIES_WINDOW = 40      # photos voisines dans le temps comparées pour les « proches » (même série)


def find_groups(entries: list, similar: bool = True) -> Report:
    """Regroupe les entrées déjà hachées (sha256 et/ou phash).

    - identiques : SHA-256 égal ;
    - quasi-identiques (≤ NEAR_MAX bits) : recherche multi-index exhaustive sur toute la source
      (4 segments de 16 bits, sondage à 1 bit : deux empreintes à distance ≤ 7 ont forcément un
      segment différent d'au plus 1 bit) — sans comparer toutes les paires ;
    - proches (≤ SIMILAR_MAX bits) : comparées parmi les photos voisines dans le temps
      (fenêtre de SERIES_WINDOW photos), ce qui correspond à une même série de prises de vue.
    """
    rep = Report(analysed=len(entries))
    by_sha: dict[str, list] = {}
    for e in entries:
        if e.sha256:
            by_sha.setdefault(e.sha256, []).append(e)
    in_exact: set[str] = set()
    for lst in by_sha.values():
        if len(lst) > 1:
            rep.groups.append(Group(IDENTICAL, sorted(lst, key=lambda e: e.path)))
            in_exact.update(e.key for e in lst[1:])   # le premier représente le groupe
    cands = [e for e in entries if e.phash and e.key not in in_exact]
    ints = [int(e.phash, 16) for e in cands]
    parent = list(range(len(cands)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    # 1) quasi-identiques : index par segment de 16 bits
    tables: list[dict[int, list[int]]] = [{} for _ in range(4)]
    for i, v in enumerate(ints):
        for s in range(4):
            tables[s].setdefault((v >> (16 * s)) & 0xFFFF, []).append(i)
    flips = [0] + [1 << b for b in range(16)]
    for i, v in enumerate(ints):
        seen = set()
        for s in range(4):
            seg = (v >> (16 * s)) & 0xFFFF
            tab = tables[s]
            for f in flips:
                for j in tab.get(seg ^ f, ()):
                    if j > i and j not in seen:
                        seen.add(j)
                        if bin(v ^ ints[j]).count("1") <= NEAR_MAX:
                            union(i, j)
    # 2) proches : voisines dans le temps
    if similar:
        order = sorted((i for i, e in enumerate(cands) if e.date is not None), key=lambda i: cands[i].date)
        for a, i in enumerate(order):
            vi = ints[i]
            for j in order[a + 1:a + 1 + SERIES_WINDOW]:
                if bin(vi ^ ints[j]).count("1") <= SIMILAR_MAX:
                    union(i, j)
    comps: dict[int, list[int]] = {}
    for i in range(len(cands)):
        comps.setdefault(find(i), []).append(i)
    for members in comps.values():
        if len(members) < 2:
            continue
        sample = members[:60]
        dmax = max(bin(ints[a] ^ ints[b]).count("1") for a in sample for b in sample if a < b)
        level = NEAR if dmax <= NEAR_MAX else SIMILAR
        rep.groups.append(Group(level, sorted((cands[i] for i in members), key=lambda e: e.path), dmax))
    order_lv = {IDENTICAL: 0, NEAR: 1, SIMILAR: 2}
    rep.groups.sort(key=lambda g: (order_lv[g.level], g.distance, -len(g.entries)))
    return rep


def hash_entries(entries: list, want_phash: bool, progress: Callable[[int, int], None],
                 cancel: threading.Event, on_hashed: Callable[[Entry], None] = lambda e: None) -> int:
    """Calcule les empreintes manquantes (lecture seule). Renvoie le nombre de fichiers illisibles."""
    bad = 0
    for i, e in enumerate(entries):
        if cancel.is_set():
            break
        changed = False
        if not e.sha256:
            e.sha256 = sha256_file(e.path, cancel=cancel)
            changed = e.sha256 is not None
            if e.sha256 is None and not cancel.is_set():
                bad += 1
        if want_phash and not e.phash and e.sha256:
            e.phash = phash_file(e.path)
            changed = changed or e.phash is not None
        if changed:
            on_hashed(e)
        progress(i + 1, len(entries))
    return bad
