"""Export des métadonnées en CSV ou JSON, vers un fichier choisi par l'utilisateur uniquement."""

from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import threading
from typing import Callable, Iterable, Optional

from . import storage

# (clé, libellé) — ordre d'export
FIELDS = [
    ("filename", "Nom du fichier"), ("path", "Chemin"), ("extension", "Extension"),
    ("width", "Largeur (px)"), ("height", "Hauteur (px)"), ("file_size", "Taille (octets)"),
    ("date", "Date"), ("taken", "Prise de vue (EXIF)"), ("caption", "Légende"), ("comment", "Commentaire"),
    ("rating", "Note"), ("event", "Événement"), ("favorite", "Favori"), ("tags", "Tags"),
    ("keywords", "Mots-clés Apple"), ("make", "Fabricant"), ("model", "Appareil"), ("lens", "Objectif"),
    ("focal", "Focale (mm)"), ("focal35", "Focale 24×36 (mm)"), ("aperture", "Ouverture"),
    ("exposure", "Temps de pose (s)"), ("iso", "ISO"), ("gps_lat", "Latitude"), ("gps_lon", "Longitude"),
    ("gps_alt", "Altitude (m)"), ("has_exif", "EXIF présent"), ("analysed", "Métadonnées analysées"),
]
DEFAULT_FIELDS = [k for k, _ in FIELDS]


def _iso(ts) -> str:
    if ts is None:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def normalize(rec: dict) -> dict:
    """Valeurs prêtes à l'export (dates ISO 8601, listes, booléens)."""
    out = dict(rec)
    out["extension"] = os.path.splitext(rec.get("filename") or "")[1].lower().lstrip(".")
    out["date"] = _iso(rec.get("date"))
    out["taken"] = _iso(rec.get("taken"))
    out["favorite"] = bool(rec.get("favorite"))
    out["tags"] = list(rec.get("tags") or [])
    out["keywords"] = list(rec.get("keywords") or [])
    out["analysed"] = rec.get("status") == "ok"
    out["has_exif"] = bool(rec.get("has_exif")) if out["analysed"] else None
    return out


def check_destination(path: str, protected: Iterable[str]) -> None:
    if not path:
        raise ValueError("Aucun fichier de destination.")
    storage.assert_outside(path, protected)
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        raise FileNotFoundError(f"Le dossier de destination n'existe pas : {d}")
    if not os.access(d, os.W_OK):
        raise PermissionError(f"Écriture impossible dans {d}")


def write(path: str, fmt: str, records: Iterable[dict], fields: Optional[list[str]] = None,
          protected: Iterable[str] = (), cancel: Optional[threading.Event] = None,
          progress: Callable[[int], None] = lambda n: None) -> int:
    """Écrit l'export (fichier temporaire puis renommage). Renvoie le nombre de lignes."""
    fields = fields or DEFAULT_FIELDS
    check_destination(path, protected)
    labels = dict(FIELDS)
    tmp = path + ".iphotor-part"
    storage.assert_outside(tmp, protected)
    n = 0
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            if fmt == "csv":
                w = csv.writer(fh, delimiter=";")
                w.writerow([labels.get(f, f) for f in fields])
                for rec in records:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError
                    r = normalize(rec)
                    w.writerow([", ".join(r[f]) if isinstance(r.get(f), list) else
                                ("oui" if r.get(f) is True else "non" if r.get(f) is False else
                                 "" if r.get(f) is None else r.get(f)) for f in fields])
                    n += 1
                    if n % 500 == 0:
                        progress(n)
            else:
                fh.write('{\n  "generateur": "iphotor",\n  "date_export": '
                         + json.dumps(_dt.datetime.now().isoformat(timespec="seconds"))
                         + ',\n  "photos": [\n')
                for rec in records:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError
                    r = normalize(rec)
                    fh.write(("    " if n == 0 else ",\n    ")
                             + json.dumps({f: r.get(f) for f in fields}, ensure_ascii=False))
                    n += 1
                    if n % 500 == 0:
                        progress(n)
                fh.write("\n  ]\n}\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    progress(n)
    return n
