"""Index des métadonnées techniques (EXIF, dimensions, GPS, taille, empreintes).

Base SQLite de CACHE (reconstructible), séparée de l'index des photos :
  - bibliothèque : <cache de la bibliothèque>/metadata.sqlite3
  - dossiers et collections : ~/.cache/photoexplorer_iphoto/dossiers/metadata.sqlite3

Incrémental (signature taille + date par fichier), reprenable (validation par lots) et
annulable. Les fichiers sources ne sont qu'ouverts en lecture (Pillow, en-têtes seulement).
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

from . import storage

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

RAW_EXTS = {".cr2", ".crw", ".nef", ".arw", ".orf", ".raf", ".dng", ".rw2", ".pef", ".srw"}
VIDEO_EXTS = {".mov", ".mpg", ".mpeg", ".avi", ".mp4", ".m4v", ".3gp", ".mts", ".wmv"}
HEIF_EXTS = {".heic", ".heif"}

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS photo_meta (
    key TEXT PRIMARY KEY,          -- GUID / « id:N » (bibliothèque) ou chemin (dossier)
    photo_id INTEGER,
    path TEXT, sig TEXT,
    status TEXT,                   -- ok | missing | error | video | unsupported
    file_size INTEGER, ext TEXT,
    width INTEGER, height INTEGER, -- dimensions affichées (orientation EXIF appliquée)
    has_exif INTEGER,
    make TEXT, model TEXT, lens TEXT,
    focal REAL, focal35 REAL, aperture REAL, exposure REAL, iso INTEGER,
    taken REAL,                    -- date de prise de vue EXIF (horodatage Unix, heure locale)
    gps_lat REAL, gps_lon REAL, gps_alt REAL,
    orientation INTEGER,
    sha256 TEXT, phash TEXT, hash_sig TEXT,
    updated REAL
);
CREATE INDEX IF NOT EXISTS idx_meta_photo ON photo_meta(photo_id);
CREATE INDEX IF NOT EXISTS idx_meta_sha ON photo_meta(sha256);
CREATE TABLE IF NOT EXISTS apple_terms (
    key TEXT, kind TEXT, value TEXT, value_norm TEXT   -- kind : keyword | person | place
);
CREATE INDEX IF NOT EXISTS idx_terms_key ON apple_terms(key);
CREATE INDEX IF NOT EXISTS idx_terms_kind ON apple_terms(kind, value_norm);
CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT);
"""

META_COLUMNS = ("key", "photo_id", "path", "sig", "status", "file_size", "ext", "width", "height",
                "has_exif", "make", "model", "lens", "focal", "focal35", "aperture", "exposure", "iso",
                "taken", "gps_lat", "gps_lon", "gps_alt", "orientation", "updated")


# ---------------------------------------------------------------------------
# Extraction (lecture seule)
# ---------------------------------------------------------------------------

def _rational(v) -> Optional[float]:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1]) if v[1] else None
        f = float(v)
        return f if f == f else None   # NaN exclu
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _text(v) -> str:
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return str(v).replace("\x00", "").strip() if v is not None else ""


def _gps(dms, ref) -> Optional[float]:
    try:
        d, m, s = (_rational(x) for x in dms)
        if d is None or m is None or s is None:
            return None
        val = d + m / 60.0 + s / 3600.0
        if _text(ref).upper() in ("S", "W"):
            val = -val
        return round(val, 7)
    except (TypeError, ValueError):
        return None


def _exif_date(text: str) -> Optional[float]:
    text = _text(text)
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return _dt.datetime.strptime(text[:19], fmt).timestamp()
        except ValueError:
            continue
    return None


def extract(path: str) -> dict:
    """Métadonnées techniques d'un fichier. N'écrit rien ; ne décode pas l'image entière."""
    out: dict = {"path": path, "ext": os.path.splitext(path)[1].lower(), "status": "ok", "has_exif": 0}
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        out["status"] = "missing"
        return out
    out["file_size"] = st.st_size
    out["sig"] = f"{st.st_size}:{int(st.st_mtime)}"
    if out["ext"] in VIDEO_EXTS:
        out["status"] = "video"
        return out
    if Image is None:
        out["status"] = "unsupported"
        return out
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with Image.open(path) as img:          # lecture paresseuse de l'en-tête
                w, h = img.size
                ex = img.getexif()
                sub = ex.get_ifd(0x8769) if ex else {}
                gps = ex.get_ifd(0x8825) if ex else {}
    except Exception as exc:  # format non reconnu (HEIC sans pillow_heif, RAW exotique…)
        out["status"] = "unsupported" if out["ext"] in HEIF_EXTS | RAW_EXTS else "error"
        out["error"] = str(exc)[:200]
        return out
    if out["ext"] in RAW_EXTS:      # Pillow lit l'aperçu ; les vraies dimensions sont dans l'EXIF
        rw, rh = sub.get(0xA002), sub.get(0xA003)
        if isinstance(rw, int) and isinstance(rh, int) and rw > 0 and rh > 0:
            w, h = rw, rh
    orient = ex.get(0x0112) if ex else None
    out["orientation"] = orient if isinstance(orient, int) else None
    if out["orientation"] in (5, 6, 7, 8):
        w, h = h, w
    out["width"], out["height"] = w, h
    if ex and (len(ex) or sub):
        out["has_exif"] = 1
    out["make"] = _text(ex.get(0x010F)) if ex else ""
    out["model"] = _text(ex.get(0x0110)) if ex else ""
    out["lens"] = _text(sub.get(0xA434)) if sub else ""
    out["focal"] = _rational(sub.get(0x920A)) if sub else None
    f35 = sub.get(0xA405) if sub else None
    out["focal35"] = float(f35) if isinstance(f35, (int, float)) and f35 > 0 else None
    ap = _rational(sub.get(0x829D)) if sub else None
    out["aperture"] = round(ap, 2) if ap else None
    out["exposure"] = _rational(sub.get(0x829A)) if sub else None
    iso = sub.get(0x8827) if sub else None
    if isinstance(iso, (tuple, list)) and iso:
        iso = iso[0]
    out["iso"] = int(iso) if isinstance(iso, (int, float)) and iso > 0 else None
    out["taken"] = _exif_date(sub.get(0x9003) or ex.get(0x0132)) if (sub or ex) else None
    if gps:
        lat = _gps(gps.get(2), gps.get(1)) if gps.get(2) else None
        lon = _gps(gps.get(4), gps.get(3)) if gps.get(4) else None
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 \
                and not (lat == 0 and lon == 0):
            out["gps_lat"], out["gps_lon"] = lat, lon
            alt = _rational(gps.get(6))
            if alt is not None:
                out["gps_alt"] = -alt if gps.get(5) == 1 else alt
    return out


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

@dataclass
class IndexProgress:
    total: int = 0
    done: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    missing: int = 0
    finished: bool = False
    cancelled: bool = False
    fatal: str = ""
    started: float = field(default_factory=time.time)


class MetadataIndex:
    """Accès à une base metadata.sqlite3 (une connexion par thread)."""

    def __init__(self, path: str, protected: Iterable[str] = ()):
        self.path = path
        self.protected = [p for p in protected if p]
        storage.assert_outside(path, self.protected)

    def connect(self) -> sqlite3.Connection:
        storage.assert_outside(self.path, self.protected)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver != SCHEMA_VERSION:
            # base de cache : reconstruite si le schéma change (aucune donnée utilisateur ici)
            con.executescript("DROP TABLE IF EXISTS photo_meta; DROP TABLE IF EXISTS apple_terms; "
                              "DROP TABLE IF EXISTS info;")
            con.executescript(SCHEMA)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            con.commit()
        return con

    # -- lecture -----------------------------------------------------------------
    def coverage(self, keys: Optional[Iterable[str]] = None) -> tuple[int, int]:
        """(entrées analysées, dont avec EXIF) — sur toutes les clés ou un sous-ensemble."""
        con = self.connect()
        try:
            if keys is None:
                r = con.execute("SELECT COUNT(*), SUM(has_exif) FROM photo_meta").fetchone()
            else:
                import json
                r = con.execute("SELECT COUNT(*), SUM(has_exif) FROM photo_meta WHERE key IN "
                                "(SELECT value FROM json_each(?))", (json.dumps(list(keys)),)).fetchone()
            return int(r[0] or 0), int(r[1] or 0)
        finally:
            con.close()

    def get(self, key: str) -> Optional[dict]:
        con = self.connect()
        try:
            r = con.execute("SELECT * FROM photo_meta WHERE key = ?", (key,)).fetchone()
            return dict(r) if r else None
        finally:
            con.close()

    # -- construction --------------------------------------------------------------
    def update(self, items: Iterable[tuple[str, Optional[int], Callable[[], Optional[str]]]],
               progress: IndexProgress, cancel: threading.Event, total: int = 0,
               batch: int = 200) -> IndexProgress:
        """Met à jour l'index. items : (clé, id de photo, fonction renvoyant le chemin du fichier).

        Les entrées dont la signature n'a pas changé sont ignorées (reprise sans tout relire).
        Validation tous les `batch` fichiers : une interruption ne perd que le lot en cours.
        """
        progress.total = total
        con = self.connect()
        try:
            known = {r[0]: r[1] for r in con.execute("SELECT key, sig FROM photo_meta")}
            rows: list[tuple] = []
            for key, photo_id, resolve in items:
                if cancel.is_set():
                    progress.cancelled = True
                    break
                progress.done += 1
                try:
                    path = resolve()
                except Exception:
                    path = None
                if not path:
                    progress.missing += 1
                    if known.get(key) != "missing":
                        rows.append(self._row({"status": "missing", "path": ""}, key, photo_id))
                    continue
                sig = storage.file_signature(path)
                if sig is not None and known.get(key) == sig:
                    progress.skipped += 1
                    continue
                data = extract(path)
                if data["status"] == "error":
                    progress.errors += 1
                elif data["status"] == "missing":
                    progress.missing += 1
                rows.append(self._row(data, key, photo_id))
                progress.updated += 1
                if len(rows) >= batch:
                    self._write(con, rows)
                    rows = []
            if rows:
                self._write(con, rows)
            con.execute("INSERT OR REPLACE INTO info VALUES ('updated', ?)", (str(time.time()),))
            con.commit()
        except Exception as exc:
            progress.fatal = str(exc)
        finally:
            con.close()
            progress.finished = True
        return progress

    @staticmethod
    def _row(d: dict, key: str, photo_id: Optional[int]) -> tuple:
        d = dict(d)
        d["key"], d["photo_id"], d["updated"] = key, photo_id, time.time()
        d.setdefault("sig", d.get("status"))
        return tuple(d.get(c) for c in META_COLUMNS)

    @staticmethod
    def _write(con: sqlite3.Connection, rows: list[tuple]) -> None:
        cols = ", ".join(META_COLUMNS)
        marks = ", ".join("?" * len(META_COLUMNS))
        # conserve les empreintes (sha256/phash) déjà calculées si le fichier n'a pas changé
        con.executemany(
            f"INSERT INTO photo_meta ({cols}) VALUES ({marks}) "
            "ON CONFLICT(key) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in META_COLUMNS[1:])
            + ", sha256 = CASE WHEN photo_meta.hash_sig = excluded.sig THEN photo_meta.sha256 END"
            + ", phash = CASE WHEN photo_meta.hash_sig = excluded.sig THEN photo_meta.phash END",
            rows)
        con.commit()

    def set_hashes(self, rows: list[tuple[str, str, Optional[str], Optional[str]]]) -> None:
        """rows : (clé, signature, sha256, phash)."""
        con = self.connect()
        try:
            con.executemany("UPDATE photo_meta SET sha256 = ?, phash = ?, hash_sig = ? WHERE key = ?",
                            [(sha, ph, sig, key) for key, sig, sha, ph in rows])
            con.commit()
        finally:
            con.close()

    def replace_terms(self, kind: str, rows: list[tuple[str, str]]) -> None:
        """Remplace les termes Apple (mots-clés, personnes, lieux) d'un type : (clé, valeur)."""
        con = self.connect()
        try:
            con.execute("DELETE FROM apple_terms WHERE kind = ?", (kind,))
            con.executemany("INSERT INTO apple_terms VALUES (?, ?, ?, ?)",
                            [(k, kind, v, storage.norm(v)) for k, v in rows])
            con.commit()
        finally:
            con.close()

    def terms_of(self, key: str) -> dict[str, list[str]]:
        con = self.connect()
        try:
            out: dict[str, list[str]] = {}
            for r in con.execute("SELECT kind, value FROM apple_terms WHERE key = ? ORDER BY value", (key,)):
                out.setdefault(r[0], []).append(r[1])
            return out
        finally:
            con.close()


def iter_rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> Iterator[sqlite3.Row]:
    cur = con.execute(sql, params)
    while True:
        chunk = cur.fetchmany(500)
        if not chunk:
            return
        yield from chunk
