#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iphotor — Explorateur en lecture seule d'une ancienne bibliothèque iPhoto ou Photos (1 à 4),
et de n'importe quel dossier d'images (mode « Explorer un dossier »).

Version 2.0

Principe fondamental : la bibliothèque iPhoto et les dossiers consultés ne sont JAMAIS modifiés.
  * Tous les fichiers de la bibliothèque sont ouverts en lecture seule ("rb").
  * Aucun fichier n'est créé, déplacé, renommé ou supprimé dans la bibliothèque.
  * L'index et le cache des miniatures sont stockés dans
    ~/.cache/photoexplorer_iphoto/ (ou $XDG_CACHE_HOME/photoexplorer_iphoto/).
  * Les miniatures des dossiers explorés vont dans ~/.cache/photoexplorer_iphoto/dossiers/.
  * Les favoris et les tags sont enregistrés dans ~/.local/share/iphotor/annotations.sqlite3.
  * Les copies (« Copier vers un dossier… ») ne lisent que les fichiers sources ; elles sont
    refusées vers une bibliothèque ou un dossier consulté et ne remplacent jamais un fichier.

Interface (1.5) : design system centralisé (PALETTES / COLORS, Theme, Icons) ; thème clair
par défaut et thème sombre facultatif ; icônes vectorielles dessinées localement avec Pillow.

Utilisation :
    ./iphotor.py                       interface graphique
    ./iphotor.py --bibliotheque CHEMIN interface graphique sur une autre bibliothèque
    ./iphotor.py --reindexer           force la reconstruction de l'index
    ./iphotor.py --verifier            contrôle de cohérence (sans interface)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import io
import json
import math
import os
import plistlib
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

# Tkinter et Pillow ne sont importés « mollement » que pour permettre
# l'utilisation de --verifier / --reindexer sans interface graphique.
try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - dépend de l'installation
    tk = None  # type: ignore

try:
    from PIL import Image, ImageDraw, ImageOps
    Image.MAX_IMAGE_PIXELS = 400_000_000  # grands scans tolérés
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

try:
    from PIL import ImageTk
except ImportError:  # pragma: no cover
    ImageTk = None  # type: ignore

# Facultatifs : HEIC via Pillow (pillow-heif) et développement des RAW (rawpy).
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    PILLOW_HEIF = True
except Exception:
    PILLOW_HEIF = False
try:
    import rawpy  # type: ignore
except Exception:
    rawpy = None  # type: ignore

# Logique des fonctionnalités d'iphotor 2 (paquet iphotorlib, sans interface graphique),
# cherché à côté du script puis dans /usr/share/iphotor (paquet Debian).
_here = os.path.dirname(os.path.realpath(__file__))
for _dir in (_here, os.path.normpath(os.path.join(_here, "..", "share", "iphotor")), "/usr/share/iphotor"):
    if os.path.isdir(os.path.join(_dir, "iphotorlib")) and _dir not in sys.path:
        sys.path.insert(0, _dir)
        break
try:
    from iphotorlib import (applemeta, collections as vcoll, deps as depcheck, duplicates as dupes,  # noqa: E402
                            export as mexport, folders as folderlib, geo, metadata as mdlib,
                            search as advsearch, stats as statlib, storage, timeline as tl)
except ImportError as _exc:  # installation incomplète : message clair plutôt qu'une trace Python
    sys.stderr.write("iphotor : le dossier « iphotorlib » est introuvable (il doit se trouver à côté de "
                     f"iphotor.py ou dans /usr/share/iphotor).\nDétail : {_exc}\n")
    sys.exit(3)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME = "iphotor"
APP_VERSION = "2.0.0"

# Emplacement par défaut (nom standard sous macOS) ; sinon, choisir via « Ouvrir une bibliothèque ».
DEFAULT_LIBRARY = os.path.expanduser("~/Pictures/iPhoto Library")
XML_NAME = "AlbumData.xml"

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "photoexplorer_iphoto",
)
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "iphotor", "config.json",
)
MAX_RECENT = 10
INDEX_SCHEMA_VERSION = "4"

PAGE_SIZE = 100            # nombre maximal de photos par page
THUMB_SIZE = 160           # taille d'affichage des vignettes (px)
THUMB_CACHE_SIZE = 200     # taille des vignettes stockées dans le cache
TILE_W = 184
TILE_H = 228
THUMB_WORKERS = 4

APPLE_EPOCH = _dt.datetime(2001, 1, 1)   # origine des « TimerInterval »

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mpg", ".mpeg", ".avi", ".mp4", ".m4v", ".3gp", ".mts", ".wmv"}
RAW_EXTS = {".cr2", ".crw", ".nef", ".arw", ".orf", ".raf", ".dng", ".rw2", ".pef"}
# RAW au format TIFF dont on sait extraire l'aperçu JPEG intégré
RAW_TIFF_EXTS = {".cr2", ".nef", ".arw", ".dng", ".pef", ".srw"}
HEIF_EXTS = {".heic", ".heif"}

# Types d'albums iPhoto -> catégorie interne
KIND_ALBUM = "album"
KIND_SMART = "smart"
KIND_FOLDER = "folder"
KIND_EVENT = "event"
KIND_SPECIAL = "special"
KIND_ALL = "all"

# Modes de l'interface
MODE_LIBRARY = "library"   # bibliothèque iPhoto / Photos
MODE_FOLDER = "folder"     # dossier indépendant
MODE_COLLECTIONS = "collections"   # albums virtuels (références multi-sources)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def normalize_text(value: str) -> str:
    """Texte normalisé pour la recherche : minuscules, sans accents."""
    if not value:
        return ""
    value = value.replace("\xa0", " ").replace(" ", " ")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.casefold()


def timer_interval_to_datetime(value) -> Optional[_dt.datetime]:
    """Convertit un « TimerInterval » Apple (secondes depuis 2001-01-01)."""
    if value is None:
        return None
    try:
        return APPLE_EPOCH + _dt.timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def format_date(value, with_time: bool = True) -> str:
    d = timer_interval_to_datetime(value)
    if d is None:
        return ""
    return d.strftime("%d/%m/%Y %H:%M") if with_time else d.strftime("%d/%m/%Y")


def format_count(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def plural(n: int, singular: str, plural_form: Optional[str] = None) -> str:
    return f"{format_count(n)} {singular if n <= 1 else (plural_form or singular + 's')}"


def file_ext(path: Optional[str]) -> str:
    return os.path.splitext(path or "")[1].lower()


def is_inside(path: str, directory: str) -> bool:
    """Vrai si `path` se trouve dans `directory` (après résolution)."""
    try:
        p = os.path.realpath(path)
        d = os.path.realpath(directory)
        return os.path.commonpath([p, d]) == d
    except ValueError:
        return False


def _as_int(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return ""
    return str(value)


# ---------------------------------------------------------------------------
# Modèle de données
# ---------------------------------------------------------------------------

@dataclass
class Photo:
    id: int
    guid: str
    caption: str
    comment: str
    rating: int
    roll_id: Optional[int]
    media_type: str
    image_path: str
    original_path: str
    thumb_path: str
    date: Optional[float]
    mod_date: Optional[float]
    filename: str
    event_name: str = ""
    size: int = 0          # taille du fichier (mode « Explorer un dossier »)
    folder: str = ""       # sous-dossier relatif au dossier ouvert ("" = le dossier lui-même)
    source: str = ""       # "folder" pour une image d'un dossier indépendant
    ref: Optional[tuple] = None   # albums virtuels : (portée, clé) de la photo d'origine

    @property
    def is_video(self) -> bool:
        return self.media_type.lower() == "movie" or file_ext(self.image_path) in VIDEO_EXTS

    @property
    def label(self) -> str:
        """Légende si elle apporte une information, sinon nom de fichier."""
        stem = os.path.splitext(self.filename)[0]
        if self.caption and self.caption != stem and self.caption != self.filename:
            return self.caption
        return self.filename or self.caption or f"Photo {self.id}"


@dataclass
class Album:
    id: int
    name: str
    kind: str
    raw_type: str
    parent_id: Optional[int]
    position: int
    photo_count: int
    date: Optional[float]
    roll_id: Optional[int]
    guid: str
    year: Optional[int]
    first_date: Optional[float] = None


# ---------------------------------------------------------------------------
# LibraryManager : accès à la bibliothèque et conversion des chemins
# ---------------------------------------------------------------------------

class LibraryManager:
    """Accès (lecture seule) à la bibliothèque et conversion des chemins macOS."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root.rstrip("/")) if root else DEFAULT_LIBRARY
        self.xml_path = os.path.join(self.root, XML_NAME)
        # Chemin de la bibliothèque tel qu'enregistré sous macOS ;
        # mis à jour depuis l'index (« Archive Path » du XML).
        self.mac_root = "/Volumes/" + os.path.basename(os.path.dirname(self.root)) + "/" + os.path.basename(self.root)
        self._lib_dirname = os.path.basename(self.root)

    # -- état --------------------------------------------------------------
    @property
    def kind(self) -> Optional[str]:
        return library_kind(self.root)

    @property
    def data_files(self) -> list[str]:
        """Fichiers dont dépend l'index : AlbumData.xml, ou la base Photos (+ son journal WAL)."""
        if self.kind == LIB_PHOTOS:
            db = photos_db_path(self.root)
            return [db, db + "-wal"] if db else []
        return [self.xml_path]

    def is_available(self) -> bool:
        files = self.data_files
        return os.path.isdir(self.root) and bool(files) and os.path.isfile(files[0])

    def unavailable_message(self) -> str:
        return (
            "La bibliothèque est inaccessible.\n\n"
            f"Chemin attendu :\n{self.root}\n\n"
            "Vérifiez que le disque externe qui contient la bibliothèque est bien "
            "branché et monté, puis utilisez « Fichier › Recharger la bibliothèque » "
            "ou « Fichier › Ouvrir une bibliothèque… »."
        )

    def set_mac_root(self, archive_path: str) -> None:
        if archive_path:
            self.mac_root = archive_path.rstrip("/")

    # -- chemins -----------------------------------------------------------
    def to_linux(self, mac_path: str) -> str:
        """Convertit un chemin macOS enregistré dans le XML en chemin Linux."""
        if not mac_path:
            return ""
        p = mac_path
        if p == self.mac_root or p.startswith(self.mac_root + "/"):
            return self.root + p[len(self.mac_root):]
        # Variante : même volume, mais casse ou nom de volume différent.
        marker = "/" + self._lib_dirname + "/"
        idx = p.find(marker)
        if p.startswith("/Volumes/") and idx >= 0:
            return self.root + p[idx + len(marker) - 1:]
        # Chemin relatif à la bibliothèque (cas rares).
        if not p.startswith("/"):
            return os.path.join(self.root, p)
        return p

    @staticmethod
    def existing(path: str) -> Optional[str]:
        """Renvoie le chemin s'il existe (en tolérant NFC/NFD), sinon None."""
        if not path:
            return None
        try:
            if os.path.isfile(path):
                return path
            for form in ("NFD", "NFC"):
                alt = unicodedata.normalize(form, path)
                if alt != path and os.path.isfile(alt):
                    return alt
        except (OSError, ValueError):
            pass
        return None

    def original_exists(self, photo: Photo) -> bool:
        """La photo (ImagePath ou OriginalPath) est-elle présente sur le disque ?"""
        for mp in (photo.image_path, photo.original_path):
            if mp and self.existing(self.to_linux(mp)):
                return True
        return False

    def display_candidates(self, photo: Photo, for_thumbnail: bool) -> list[tuple[str, str]]:
        """Liste ordonnée (chemin Linux, origine) des fichiers affichables."""
        out: list[tuple[str, str]] = []
        order = (
            [("thumb", photo.thumb_path), ("image", photo.image_path), ("original", photo.original_path)]
            if for_thumbnail else
            [("image", photo.image_path), ("original", photo.original_path), ("thumb", photo.thumb_path)]
        )
        for origin, mp in order:
            if not mp:
                continue
            if not is_displayable(mp):
                continue  # vidéo, RAW non TIFF… : non décodés
            out.append((self.to_linux(mp), origin))
        return out

    def find_display_file(self, photo: Photo, for_thumbnail: bool) -> tuple[Optional[str], str]:
        for path, origin in self.display_candidates(photo, for_thumbnail):
            real = self.existing(path)
            if real:
                return real, origin
        return None, ""

    def assert_outside(self, path: str) -> None:
        """Garde-fou : refuse toute écriture dans la bibliothèque."""
        if is_inside(path, self.root):
            raise PermissionError(
                f"Écriture refusée : {path} se trouve dans la bibliothèque iPhoto "
                "(la bibliothèque est en lecture seule)."
            )


# ---------------------------------------------------------------------------
# XMLParser : lecture et interprétation d'AlbumData.xml
# ---------------------------------------------------------------------------

class XMLParser:
    """Lit AlbumData.xml (plist XML) et produit des structures normalisées."""

    def __init__(self, xml_path: str):
        self.xml_path = xml_path
        self.data: dict = {}
        self.warnings: list[str] = []

    def load(self) -> None:
        with open(self.xml_path, "rb") as fh:          # lecture seule
            data = plistlib.load(fh)
        if not isinstance(data, dict):
            raise ValueError("AlbumData.xml n'a pas la structure attendue (dictionnaire racine absent).")
        self.data = data

    @property
    def archive_path(self) -> str:
        return _as_str(self.data.get("Archive Path"))

    @property
    def app_version(self) -> str:
        return _as_str(self.data.get("Application Version"))

    @staticmethod
    def key_list(entry: dict) -> list[int]:
        """Liste des identifiants de photos (KeyList ou KeyListString)."""
        raw = entry.get("KeyList")
        if raw is None:
            raw = entry.get("KeyListString")
        keys: list[int] = []
        if isinstance(raw, (list, tuple)):
            for k in raw:
                v = _as_int(k)
                if v is not None:
                    keys.append(v)
        elif isinstance(raw, (str, bytes)):
            for k in re.findall(r"\d+", _as_str(raw)):
                keys.append(int(k))
        return keys

    def photos(self) -> Iterable[dict]:
        master = self.data.get("Master Image List") or {}
        if not isinstance(master, dict):
            self.warnings.append("« Master Image List » absente ou invalide.")
            return
        for key, m in master.items():
            pid = _as_int(key)
            if pid is None or not isinstance(m, dict):
                self.warnings.append(f"Photo ignorée (entrée invalide) : {key!r}")
                continue
            image_path = _as_str(m.get("ImagePath"))
            original_path = _as_str(m.get("OriginalPath"))
            filename = os.path.basename(original_path or image_path)
            yield {
                "id": pid,
                "guid": _as_str(m.get("GUID")),
                "caption": _as_str(m.get("Caption")).strip(),
                "comment": _as_str(m.get("Comment")).strip(),
                "rating": _as_int(m.get("Rating")) or 0,
                "roll_id": _as_int(m.get("Roll")),
                "media_type": _as_str(m.get("MediaType")) or "Image",
                "image_path": image_path,
                "original_path": original_path,
                "thumb_path": _as_str(m.get("ThumbPath")),
                "date": _as_float(m.get("DateAsTimerInterval")),
                "mod_date": _as_float(m.get("MetaModDateAsTimerInterval")) or _as_float(m.get("ModDateAsTimerInterval")),
                "filename": filename,
            }

    def rolls(self) -> list[dict]:
        out = []
        for r in self.data.get("List of Rolls") or []:
            if not isinstance(r, dict):
                continue
            rid = _as_int(r.get("RollID"))
            if rid is None:
                self.warnings.append("Événement (Roll) sans RollID ignoré.")
                continue
            out.append({
                "roll_id": rid,
                "uuid": _as_str(r.get("ProjectUuid")),
                "name": _as_str(r.get("RollName")),
                "date": _as_float(r.get("RollDateAsTimerInterval")),
                "keys": self.key_list(r),
            })
        return out

    def albums(self) -> list[dict]:
        out = []
        for pos, a in enumerate(self.data.get("List of Albums") or []):
            if not isinstance(a, dict):
                continue
            aid = _as_int(a.get("AlbumId"))
            if aid is None:
                self.warnings.append(f"Album sans AlbumId ignoré : {a.get('AlbumName')!r}")
                continue
            out.append({
                "id": aid,
                "name": _as_str(a.get("AlbumName")).strip() or "(sans nom)",
                "raw_type": _as_str(a.get("Album Type")) or "Regular",
                "parent_id": _as_int(a.get("Parent")),
                "guid": _as_str(a.get("GUID")),
                "position": pos,
                "xml_count": _as_int(a.get("PhotoCount")),
                "date": _as_float(a.get("ProjectEarliestDateAsTimerInterval")),
                "keys": self.key_list(a),
            })
        return out


# ---------------------------------------------------------------------------
# PhotosDBParser : bibliothèques de l'app Photos 1 à 4 (macOS 10.10 à 10.14)
# ---------------------------------------------------------------------------

FR_MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")

# Identifiants des dossiers / événements / albums spéciaux dans l'index : les
# tables RKFolder, RKMoment et RKAlbum ont chacune leur propre numérotation.
PHOTOS_FOLDER_BASE = 10_000_000
PHOTOS_EVENT_BASE = 20_000_000
PHOTOS_SPECIAL_BASE = 30_000_000


def format_day_fr(value) -> str:
    d = timer_interval_to_datetime(value)
    if d is None:
        return ""
    return f"{d.day} {FR_MONTHS[d.month - 1]} {d.year}"


class PhotosDBParser:
    """Lit la base SQLite d'une bibliothèque Photos 1 à 4 (tables RK* héritées d'Aperture).

    La base n'est jamais ouverte dans la bibliothèque : elle est d'abord copiée,
    avec son journal WAL, dans le dossier de cache, puis lue depuis cette copie.
    Le schéma n'est pas documenté par Apple ; les champs absents sont tolérés.
    """

    def __init__(self, root: str, workdir: str, assert_outside: Callable[[str], None]):
        self.root = root
        self.workdir = workdir
        self.assert_outside = assert_outside
        self.warnings: list[str] = []
        self._tmpdir = ""
        self._photos: list[dict] = []
        self._albums: list[dict] = []
        self._rolls: list[dict] = []
        self.archive_path = ""
        self.app_version = "Photos"

    # -- lecture -----------------------------------------------------------
    def load(self) -> None:
        db = photos_db_path(self.root)
        if not db:
            raise FileNotFoundError("Base de la bibliothèque Photos introuvable (database/photos.db).")
        try:
            with open(os.path.join(self.root, "database", "DataModelVersion.plist"), "rb") as fh:
                dmv = plistlib.load(fh)
            self.app_version = f"Photos (schéma {dmv.get('LibrarySchemaVersion', '?')})"
        except (OSError, plistlib.InvalidFileException, ValueError):
            pass

        # Copie de la base (et du journal WAL) hors de la bibliothèque
        self._tmpdir = os.path.join(self.workdir, "photosdb.tmp")
        self.assert_outside(self._tmpdir)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.makedirs(self._tmpdir)
        copy = os.path.join(self._tmpdir, "photos.db")
        shutil.copyfile(db, copy)
        if os.path.isfile(db + "-wal"):
            shutil.copyfile(db + "-wal", copy + "-wal")
        con = sqlite3.connect(copy)
        con.row_factory = sqlite3.Row
        try:
            self._read(con)
        finally:
            con.close()

    def cleanup(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _columns(con, table: str) -> set[str]:
        try:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    @staticmethod
    def _res_path(kind: str, rid: int) -> str:
        return f"resources/media/{kind}/{(rid >> 8) & 255:02x}/{(rid >> 16) & 255:02x}/fullsizeoutput_{rid:x}.jpeg"

    @staticmethod
    def _thumb_path(vid: int, adjustment: str) -> str:
        return (f"resources/proxies/derivatives/{(vid >> 8) & 255:02x}/{(vid >> 16) & 255:02x}/"
                f"{vid:x}/{adjustment or 'UNADJUSTEDNONRAW'}_thumb_{vid:x}.jpg")

    def _moment_name(self, blob, date) -> str:
        names: list[str] = []
        if blob:
            try:
                data = plistlib.loads(blob)
                for key in ("compoundNames", "compoundSecondaryNames"):
                    part = data.get(key) or {}
                    sorted_names = [n for n in part.get("sortedNames") or [] if n]
                    if sorted_names:
                        label = ", ".join(sorted_names)
                        if part.get("nameSuffix"):
                            label += f" ({part['nameSuffix']})"
                        names.append(label)
                        break
            except Exception:
                pass
        day = format_day_fr(date)
        if names:
            return f"{names[0]} — {day}" if day else names[0]
        return day or "Moment sans date"

    def _read(self, con) -> None:
        vcols = self._columns(con, "RKVersion")
        mcols = self._columns(con, "RKMaster")
        if not vcols or not mcols:
            raise ValueError("Base Photos non reconnue (tables RKVersion / RKMaster absentes).")

        def col(table_cols, alias, name, default="NULL"):
            return f"{alias}.{name}" if name in table_cols else default

        # Ressources générées par Photos (rendus JPEG pleine taille)
        version_res: dict[int, int] = {}
        master_res: dict[int, int] = {}
        if self._columns(con, "RKModelResource"):
            for r in con.execute(
                    "SELECT modelId, attachedModelId, attachedModelType FROM RKModelResource "
                    "WHERE resourceType = 4 AND UTI = 'public.jpeg' ORDER BY modelId"):
                (version_res if r["attachedModelType"] == 2 else master_res)[r["attachedModelId"]] = r["modelId"]

        # Aperçus « Previews/<…>/<uuid>/<fichier>.jpg » (anciennes bibliothèques)
        previews: dict[str, str] = {}
        prev_dir = os.path.join(self.root, "Previews")
        if os.path.isdir(prev_dir):
            for dirpath, _dirs, files in os.walk(prev_dir):
                for f in files:
                    if file_ext(f) in IMAGE_EXTS:
                        rel = os.path.relpath(os.path.join(dirpath, f), self.root)
                        previews[os.path.basename(dirpath)] = rel
        has_derivatives = os.path.isdir(os.path.join(self.root, "resources", "proxies", "derivatives"))

        # Moments -> événements
        moments: dict[str, dict] = {}
        if self._columns(con, "RKMoment"):
            for r in con.execute("SELECT modelId, uuid, startDate, reverseLocationData FROM RKMoment"):
                moments[r["uuid"]] = {"id": PHOTOS_EVENT_BASE + r["modelId"], "uuid": r["uuid"],
                                      "start": r["startDate"], "blob": r["reverseLocationData"], "keys": []}

        where = ["v.isInTrash = 0"] if "isInTrash" in vcols else []
        if "showInLibrary" in vcols:
            where.append("v.showInLibrary = 1")
        sql = f"""
            SELECT v.modelId AS vid, v.uuid AS vuuid, {col(vcols, 'v', 'name')} AS title,
                   {col(vcols, 'v', 'extendedDescription')} AS descr, {col(vcols, 'v', 'mainRating', '0')} AS rating,
                   {col(vcols, 'v', 'isFavorite', '0')} AS fav, {col(vcols, 'v', 'type', '2')} AS vtype,
                   {col(vcols, 'v', 'fileName')} AS vfile, {col(vcols, 'v', 'imageDate')} AS idate,
                   {col(vcols, 'v', 'imageTimeZoneOffsetSeconds', '0')} AS tz,
                   {col(vcols, 'v', 'lastModifiedDate')} AS mdate, {col(vcols, 'v', 'adjustmentUuid')} AS adj,
                   {col(vcols, 'v', 'momentUuid')} AS moment,
                   m.modelId AS mid, {col(mcols, 'm', 'imagePath')} AS mpath, {col(mcols, 'm', 'fileName')} AS mfile,
                   {col(mcols, 'm', 'UTI')} AS uti, {col(mcols, 'm', 'fileIsReference', '0')} AS ref
            FROM RKVersion v LEFT JOIN RKMaster m ON m.modelId = v.masterId
            {"WHERE " + " AND ".join(where) if where else ""}
            ORDER BY v.imageDate, v.modelId"""
        self._fav: list[int] = []
        by_uuid: dict[str, int] = {}
        for r in con.execute(sql):
            vid = r["vid"]
            master_rel = ""
            if r["mpath"] and not r["ref"]:
                master_rel = "Masters/" + r["mpath"].lstrip("/")
            image_rel = ""
            if vid in version_res:
                image_rel = self._res_path("version", version_res[vid])
            elif r["vuuid"] in previews:
                image_rel = previews[r["vuuid"]]
            elif r["mid"] in master_res and not (master_rel and file_ext(master_rel) in IMAGE_EXTS | HEIF_EXTS):
                image_rel = self._res_path("master", master_res[r["mid"]])
            else:
                image_rel = master_rel or (self._res_path("master", master_res[r["mid"]])
                                           if r["mid"] in master_res else "")
            original_rel = master_rel if master_rel != image_rel else ""
            thumb_rel = self._thumb_path(vid, r["adj"]) if has_derivatives else ""
            date = r["idate"] + (r["tz"] or 0) if r["idate"] is not None else None
            moment = moments.get(r["moment"]) if r["moment"] else None
            caption = (_as_str(r["title"])).strip()
            filename = _as_str(r["vfile"]) or _as_str(r["mfile"]) or os.path.basename(master_rel)
            self._photos.append({
                "id": vid, "guid": _as_str(r["vuuid"]), "caption": caption or os.path.splitext(filename)[0],
                "comment": _as_str(r["descr"]).strip(), "rating": _as_int(r["rating"]) or 0,
                "roll_id": moment["id"] if moment else None,
                "media_type": "Movie" if r["vtype"] == 8 else "Image",
                "image_path": image_rel, "original_path": original_rel, "thumb_path": thumb_rel,
                "date": date, "mod_date": _as_float(r["mdate"]), "filename": filename,
            })
            by_uuid[_as_str(r["vuuid"])] = vid
            if moment is not None:
                moment["keys"].append(vid)
                if date is not None and (moment.get("date") is None or date < moment["date"]):
                    moment["date"] = date
            if r["fav"]:
                self._fav.append(vid)
        photo_ids = {p["id"] for p in self._photos}

        # Événements : moments non vides ; les moments d'une même journée qui portent
        # le même nom (même lieu, ou lieu inconnu) sont regroupés en un seul événement.
        merged: dict[str, dict] = {}
        remap: dict[int, int] = {}
        for m in sorted(moments.values(), key=lambda m: (m.get("date", m["start"]) or 0, m["id"])):
            if not m["keys"]:
                continue
            date = m.get("date", m["start"])
            name = self._moment_name(m["blob"], date)
            ev = merged.get(name)
            if ev is None:
                ev = merged[name] = {"roll_id": m["id"], "uuid": m["uuid"], "name": name, "date": date,
                                     "keys": []}
            ev["keys"].extend(m["keys"])
            remap[m["id"]] = ev["roll_id"]
        self._rolls = sorted(merged.values(), key=lambda r: (r["date"] or 0, r["roll_id"]))
        for p in self._photos:
            if p["roll_id"] is not None:
                p["roll_id"] = remap.get(p["roll_id"], p["roll_id"])

        # Dossiers créés par l'utilisateur
        folders: dict[str, dict] = {}
        fcols = self._columns(con, "RKFolder")
        if fcols:
            cond = ["isMagic = 0"] if "isMagic" in fcols else []
            if "isInTrash" in fcols:
                cond.append("isInTrash = 0")
            if "folderType" in fcols:
                cond.append("folderType = 1")
            for r in con.execute("SELECT modelId, uuid, name, parentFolderUuid FROM RKFolder"
                                 + (" WHERE " + " AND ".join(cond) if cond else "")):
                folders[r["uuid"]] = {"id": PHOTOS_FOLDER_BASE + r["modelId"], "name": _as_str(r["name"]),
                                      "parent_uuid": r["parentFolderUuid"], "keys": [], "children": []}

        # Ordre personnalisé des albums
        custom: dict[str, dict[str, int]] = {}
        if self._columns(con, "RKCustomSortOrder"):
            for r in con.execute("SELECT containerUuid, objectUuid, orderNumber FROM RKCustomSortOrder"):
                custom.setdefault(r["containerUuid"], {})[r["objectUuid"]] = r["orderNumber"]
        uuid_of = {v: u for u, v in by_uuid.items()}

        albums: list[dict] = []
        acols = self._columns(con, "RKAlbum")
        if acols:
            cond = ["name IS NOT NULL", "name != ''"]
            for c_, v_ in (("isMagic", 0), ("isInTrash", 0), ("albumSubclass", 3), ("albumType", 1)):
                if c_ in acols:
                    cond.append(f"{c_} = {v_}")
            members: dict[int, list[int]] = {}
            for r in con.execute("SELECT albumId, versionId FROM RKAlbumVersion ORDER BY modelId"):
                if r["versionId"] in photo_ids:
                    members.setdefault(r["albumId"], []).append(r["versionId"])
            date_of = {p["id"]: (p["date"] or 0) for p in self._photos}
            for r in con.execute("SELECT modelId, uuid, name, folderUuid FROM RKAlbum WHERE " + " AND ".join(cond)):
                keys = members.get(r["modelId"], [])
                order = custom.get(r["uuid"])
                if order:
                    keys = sorted(keys, key=lambda k: (order.get(uuid_of.get(k, ""), 1 << 30), date_of.get(k, 0)))
                else:
                    keys = sorted(keys, key=lambda k: (date_of.get(k, 0), k))
                parent = folders.get(r["folderUuid"])
                albums.append({"id": r["modelId"], "name": _as_str(r["name"]), "raw_type": "Regular",
                               "parent_id": parent["id"] if parent else None, "guid": _as_str(r["uuid"]),
                               "date": None, "keys": keys, "folder_uuid": r["folderUuid"]})

        # Contenu des dossiers : photos de leurs albums et sous-dossiers
        for f in folders.values():
            parent = folders.get(f["parent_uuid"])
            f["parent_id"] = parent["id"] if parent else None
            if parent:
                parent["children"].append(f)
        for a in albums:
            f = folders.get(a.pop("folder_uuid"))
            while f is not None:
                f["keys"].extend(a["keys"])
                f = folders.get(f["parent_uuid"])

        pos = 0
        out = []
        for f in sorted(folders.values(), key=lambda f: normalize_text(f["name"])):
            out.append({"id": f["id"], "name": f["name"] or "(sans nom)", "raw_type": "Folder",
                        "parent_id": f["parent_id"], "guid": "", "position": 0, "xml_count": None,
                        "date": None, "keys": f["keys"]})
        for a in sorted(albums, key=lambda a: normalize_text(a["name"])):
            a.update(position=0, xml_count=None)
            out.append(a)
        if self._fav:
            out.append({"id": PHOTOS_SPECIAL_BASE + 1, "name": "Favoris", "raw_type": "Flagged",
                        "parent_id": None, "guid": "favorites", "position": 0, "xml_count": None,
                        "date": None, "keys": self._fav})
        for r in self._rolls:
            out.append({"id": r["roll_id"], "name": r["name"], "raw_type": "Event", "parent_id": None,
                        "guid": r["uuid"], "position": 0, "xml_count": None, "date": r["date"],
                        "keys": r["keys"]})
        for a in out:
            a["position"] = pos
            pos += 1
        self._albums = out

    # -- interface commune avec XMLParser ------------------------------------
    def photos(self) -> Iterable[dict]:
        return iter(self._photos)

    def rolls(self) -> list[dict]:
        return self._rolls

    def albums(self) -> list[dict]:
        return self._albums


# ---------------------------------------------------------------------------
# CacheManager : index SQLite local et cache de miniatures
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE photos (
    id INTEGER PRIMARY KEY,
    guid TEXT, caption TEXT, comment TEXT, rating INTEGER,
    roll_id INTEGER, media_type TEXT,
    image_path TEXT, original_path TEXT, thumb_path TEXT,
    date REAL, mod_date REAL, filename TEXT,
    event_name TEXT, search TEXT
);
CREATE TABLE albums (
    id INTEGER PRIMARY KEY,
    name TEXT, name_norm TEXT, kind TEXT, raw_type TEXT,
    parent_id INTEGER, position INTEGER, photo_count INTEGER,
    date REAL, roll_id INTEGER, guid TEXT, year INTEGER,
    first_date REAL
);
CREATE TABLE album_photos (
    album_id INTEGER, position INTEGER, photo_id INTEGER,
    PRIMARY KEY (album_id, position)
) WITHOUT ROWID;
CREATE INDEX idx_photos_date ON photos(date, id);
CREATE INDEX idx_photos_roll ON photos(roll_id);
CREATE INDEX idx_album_photos_photo ON album_photos(photo_id);
CREATE INDEX idx_albums_parent ON albums(parent_id, position);
CREATE INDEX idx_albums_kind ON albums(kind, year);
CREATE INDEX idx_albums_roll ON albums(roll_id);
"""


class CacheManager:
    """Gestion de l'index local (SQLite) et du cache des miniatures."""

    def __init__(self, library: LibraryManager, cache_dir: Optional[str] = None):
        self.library = library
        cache_dir = cache_dir or library_cache_dir(library.root)
        self.cache_dir = cache_dir
        library.assert_outside(cache_dir)
        self.index_path = os.path.join(cache_dir, "index.sqlite3")
        self.thumb_dir = os.path.join(cache_dir, "thumbs")

    def ensure_dirs(self) -> None:
        self.library.assert_outside(self.cache_dir)
        os.makedirs(self.thumb_dir, exist_ok=True)

    # -- validité de l'index ---------------------------------------------
    def xml_signature(self) -> Optional[tuple[str, str]]:
        """Taille et date des fichiers de données (AlbumData.xml ou base Photos + WAL)."""
        files = self.library.data_files
        sizes, mtimes = [], []
        for i, path in enumerate(files):
            try:
                st = os.stat(path)
            except OSError:
                if i == 0:
                    return None
                continue   # pas de journal WAL : normal
            sizes.append(str(st.st_size))
            mtimes.append(str(int(st.st_mtime)))
        if not sizes:
            return None
        return "+".join(sizes), "+".join(mtimes)

    def read_meta(self) -> dict:
        if not os.path.isfile(self.index_path):
            return {}
        try:
            con = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
            try:
                return dict(con.execute("SELECT key, value FROM meta").fetchall())
            finally:
                con.close()
        except sqlite3.Error:
            return {}

    def index_exists(self) -> bool:
        meta = self.read_meta()
        return meta.get("schema") == INDEX_SCHEMA_VERSION and meta.get("complete") == "1"

    def is_valid(self) -> bool:
        """L'index existe et correspond à la taille / date de AlbumData.xml."""
        meta = self.read_meta()
        sig = self.xml_signature()
        if not meta or sig is None:
            return False
        return (
            meta.get("schema") == INDEX_SCHEMA_VERSION
            and meta.get("complete") == "1"
            and meta.get("xml_size") == sig[0]
            and meta.get("xml_mtime") == sig[1]
            and meta.get("library_root") == self.library.root
        )

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.index_path)
        con.row_factory = sqlite3.Row
        return con

    # -- construction de l'index -----------------------------------------
    def build(self, progress: Callable[[str], None] = lambda msg: None) -> dict:
        """Lit la bibliothèque (AlbumData.xml ou base Photos) et (re)construit l'index."""
        self.ensure_dirs()
        sig = self.xml_signature()
        if sig is None:
            raise FileNotFoundError(self.library.unavailable_message())

        t0 = time.time()
        if self.library.kind == LIB_PHOTOS:
            progress("Lecture de la base de la bibliothèque Photos…")
            parser = PhotosDBParser(self.library.root, workdir=self.cache_dir,
                                    assert_outside=self.library.assert_outside)
        else:
            progress("Lecture de AlbumData.xml (patientez quelques secondes pour une grande bibliothèque)…")
            parser = XMLParser(self.library.xml_path)
        try:
            parser.load()
        finally:
            if hasattr(parser, "cleanup"):
                parser.cleanup()

        tmp_path = self.index_path + ".tmp"
        self.library.assert_outside(tmp_path)
        for p in (tmp_path, tmp_path + "-journal"):
            if os.path.exists(p):
                os.remove(p)
        con = sqlite3.connect(tmp_path)
        try:
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            con.executescript(SCHEMA)

            # --- événements (Rolls) : pour nommer les événements des photos
            progress("Analyse des événements…")
            rolls = parser.rolls()
            roll_by_id = {r["roll_id"]: r for r in rolls}
            roll_by_uuid = {r["uuid"]: r for r in rolls if r["uuid"]}

            # --- photos
            progress("Indexation des photos…")
            rows = []
            photo_ids: set[int] = set()
            for p in parser.photos():
                roll = roll_by_id.get(p["roll_id"]) if p["roll_id"] is not None else None
                event_name = roll["name"] if roll else ""
                search = normalize_text(" ".join((p["caption"], p["comment"], p["filename"], event_name)))
                rows.append((
                    p["id"], p["guid"], p["caption"], p["comment"], p["rating"],
                    p["roll_id"], p["media_type"], p["image_path"], p["original_path"],
                    p["thumb_path"], p["date"], p["mod_date"], p["filename"], event_name, search,
                ))
                photo_ids.add(p["id"])
                if len(rows) >= 5000:
                    con.executemany("INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    rows.clear()
                    progress(f"Indexation des photos… {format_count(len(photo_ids))}")
            if rows:
                con.executemany("INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

            # --- albums, dossiers, événements
            progress("Indexation des albums, dossiers et événements…")
            albums = parser.albums()
            album_ids = {a["id"] for a in albums}
            stale_keys = 0
            event_without_roll = 0
            for a in albums:
                raw = a["raw_type"]
                if raw == "Folder":
                    kind = KIND_FOLDER
                elif raw == "Event":
                    kind = KIND_EVENT
                elif raw == "Smart":
                    kind = KIND_SMART
                elif raw == "Regular":
                    kind = KIND_ALBUM
                elif raw == "99" or a["guid"] == "allPhotosAlbum":
                    kind = KIND_ALL
                else:
                    kind = KIND_SPECIAL

                roll_id = None
                date = a["date"]
                keys = a["keys"]
                if kind == KIND_EVENT:
                    roll = roll_by_uuid.get(a["guid"])
                    if roll is None:
                        # Repli : même nom et mêmes photos
                        roll = next((r for r in rolls if r["name"] == a["name"] and r["keys"] == keys), None)
                    if roll is not None:
                        roll_id = roll["roll_id"]
                        if date is None:
                            date = roll["date"]
                        if not keys:
                            keys = roll["keys"]
                    else:
                        event_without_roll += 1

                # Photos valides, sans doublon, dans l'ordre d'iPhoto
                seen: set[int] = set()
                valid: list[int] = []
                for k in keys:
                    if k in photo_ids and k not in seen:
                        seen.add(k)
                        valid.append(k)
                    elif k not in photo_ids:
                        stale_keys += 1

                # Date d'un événement : à défaut, date de sa première photo
                if kind == KIND_EVENT and date is None and valid:
                    row = con.execute("SELECT MIN(date) FROM photos WHERE id IN (%s)" %
                                      ",".join(str(x) for x in valid[:500])).fetchone()
                    date = row[0] if row else None
                dt = timer_interval_to_datetime(date)
                year = dt.year if dt else None

                parent = a["parent_id"] if a["parent_id"] in album_ids else None
                con.execute(
                    "INSERT OR REPLACE INTO albums VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a["id"], a["name"], normalize_text(a["name"]), kind, raw, parent,
                     a["position"], len(valid), date, roll_id, a["guid"], year, None),
                )
                if kind != KIND_ALL:   # « Toutes les photos » est calculé à part
                    con.executemany(
                        "INSERT INTO album_photos VALUES (?,?,?)",
                        ((a["id"], i, pid) for i, pid in enumerate(valid)),
                    )

            # Date de la première photo de chaque album (sert à distinguer les homonymes)
            con.execute("CREATE INDEX idx_tmp_ap ON album_photos(album_id)")
            con.execute("UPDATE albums SET first_date = (SELECT MIN(p.date) FROM album_photos ap "
                        "JOIN photos p ON p.id = ap.photo_id WHERE ap.album_id = albums.id)")
            con.execute("DROP INDEX idx_tmp_ap")

            meta = {
                "schema": INDEX_SCHEMA_VERSION,
                "xml_size": sig[0],
                "xml_mtime": sig[1],
                "library_root": self.library.root,
                "archive_path": parser.archive_path,
                "app_version": parser.app_version,
                "built_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "photo_count": str(len(photo_ids)),
                "album_count": str(len(albums)),
                "roll_count": str(len(rolls)),
                "stale_keys": str(stale_keys),
                "warnings": "\n".join(parser.warnings[:200]),
                "complete": "1",
            }
            con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
            con.commit()
        finally:
            con.close()
        os.replace(tmp_path, self.index_path)
        del parser
        stats = {
            "photos": len(photo_ids), "albums": len(albums), "rolls": len(rolls),
            "stale_keys": stale_keys, "event_without_roll": event_without_roll,
            "seconds": round(time.time() - t0, 1),
        }
        progress(f"Index construit en {stats['seconds']} s.")
        return stats

    # -- cache des miniatures -----------------------------------------------
    def thumb_cache_path(self, photo: Photo) -> str:
        stamp = int(photo.mod_date or 0)
        sub = f"{photo.id % 256:02x}"
        return os.path.join(self.thumb_dir, sub, f"{photo.id}_{stamp}.jpg")

    def clear_thumbnails(self) -> None:
        self.library.assert_outside(self.thumb_dir)
        if os.path.isdir(self.thumb_dir):
            shutil.rmtree(self.thumb_dir, ignore_errors=True)
        os.makedirs(self.thumb_dir, exist_ok=True)

    def thumbnails_size(self) -> tuple[int, int]:
        total = count = 0
        for dirpath, _dirs, files in os.walk(self.thumb_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                    count += 1
                except OSError:
                    pass
        return count, total


# ---------------------------------------------------------------------------
# Plusieurs bibliothèques : caches séparés, préférences, détection
# ---------------------------------------------------------------------------

def library_cache_dir(root: str) -> str:
    """Dossier de cache propre à une bibliothèque (index + miniatures)."""
    root = os.path.abspath(root.rstrip("/"))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(root)).strip("_")[:40] or "bibliotheque"
    digest = hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()[:10]
    return os.path.join(CACHE_DIR, "bibliotheques", f"{slug}-{digest}")


def migrate_legacy_cache() -> None:
    """Déplace le cache de la version 1.0.0 (un seul index) vers le dossier de sa bibliothèque."""
    legacy_index = os.path.join(CACHE_DIR, "index.sqlite3")
    legacy_thumbs = os.path.join(CACHE_DIR, "thumbs")
    if not os.path.isfile(legacy_index):
        return
    try:
        con = sqlite3.connect(f"file:{legacy_index}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='library_root'").fetchone()
        finally:
            con.close()
        root = row[0] if row else ""
        if not root:
            return
        target = library_cache_dir(root)
        os.makedirs(target, exist_ok=True)
        if not os.path.exists(os.path.join(target, "index.sqlite3")):
            os.replace(legacy_index, os.path.join(target, "index.sqlite3"))
        if os.path.isdir(legacy_thumbs) and not os.path.exists(os.path.join(target, "thumbs")):
            os.replace(legacy_thumbs, os.path.join(target, "thumbs"))
    except (sqlite3.Error, OSError) as exc:
        sys.stderr.write(f"[cache] migration de l'ancien index impossible : {exc}\n")


class Settings:
    """Préférences de l'utilisateur (~/.config/iphotor/config.json)."""

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self.last_library: str = ""
        self.recent: list[str] = []
        self.last_folder: str = ""
        self.folder_recursive: bool = False
        self.folder_sort: str = ""
        self.copy_dest: str = ""
        self.copy_original: bool = False
        self.copy_open: bool = False
        self.theme: str = "light"
        self.recent_folders: list[str] = []
        self.folder_favorites: list[str] = []
        self.folder_watch: bool = False
        self.slideshow_interval: int = 4
        self.slideshow_loop: bool = True
        self.slideshow_info: bool = True
        self.slideshow_order: str = "chrono"
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.last_library = _as_str(data.get("last_library"))
            self.recent = [_as_str(p) for p in data.get("recent", []) if p][:MAX_RECENT]
            self.last_folder = _as_str(data.get("last_folder"))
            self.folder_recursive = bool(data.get("folder_recursive", False))
            self.folder_sort = _as_str(data.get("folder_sort"))
            self.copy_dest = _as_str(data.get("copy_dest"))
            self.copy_original = bool(data.get("copy_original", False))
            self.copy_open = bool(data.get("copy_open", False))
            theme = _as_str(data.get("theme"))
            self.theme = theme if theme in ("light", "dark") else "light"
            self.recent_folders = [_as_str(p) for p in data.get("recent_folders", []) if p][:12]
            self.folder_favorites = [_as_str(p) for p in data.get("folder_favorites", []) if p]
            self.folder_watch = bool(data.get("folder_watch", False))
            try:
                self.slideshow_interval = max(1, min(60, int(data.get("slideshow_interval", 4))))
            except (TypeError, ValueError):
                self.slideshow_interval = 4
            self.slideshow_loop = bool(data.get("slideshow_loop", True))
            self.slideshow_info = bool(data.get("slideshow_info", True))
            self.slideshow_order = _as_str(data.get("slideshow_order")) or "chrono"

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"last_library": self.last_library, "recent": self.recent,
                           "last_folder": self.last_folder, "folder_recursive": self.folder_recursive,
                           "folder_sort": self.folder_sort, "copy_dest": self.copy_dest,
                           "copy_original": self.copy_original, "copy_open": self.copy_open,
                           "theme": self.theme, "recent_folders": self.recent_folders,
                           "folder_favorites": self.folder_favorites, "folder_watch": self.folder_watch,
                           "slideshow_interval": self.slideshow_interval, "slideshow_loop": self.slideshow_loop,
                           "slideshow_info": self.slideshow_info, "slideshow_order": self.slideshow_order},
                          fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            sys.stderr.write(f"[préférences] enregistrement impossible : {exc}\n")

    def remember(self, root: str) -> None:
        root = os.path.abspath(root.rstrip("/"))
        self.last_library = root
        self.recent = [root] + [p for p in self.recent if p != root]
        self.recent = self.recent[:MAX_RECENT]
        self.save()


LIB_IPHOTO = "iphoto"      # bibliothèque iPhoto lisible (AlbumData.xml)
LIB_PHOTOS = "photos"      # app Photos 1 à 4 (macOS 10.10 à 10.14) : base photos.db / Library.apdb
LIB_PHOTOS_NEW = "photos5" # app Photos 5 et suivantes (Photos.sqlite) : non prise en charge


def photos_db_path(root: str) -> Optional[str]:
    """Base principale d'une bibliothèque Photos 1 à 4, si elle existe."""
    for name in ("photos.db", "Library.apdb"):
        p = os.path.join(root, "database", name)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            pass
    return None


def library_kind(path: str) -> Optional[str]:
    try:
        if os.path.isfile(os.path.join(path, XML_NAME)):
            return LIB_IPHOTO
        if os.path.isfile(os.path.join(path, "database", "Photos.sqlite")):
            return LIB_PHOTOS_NEW
        if photos_db_path(path):
            return LIB_PHOTOS
        if path.lower().endswith(".photoslibrary") and os.path.isdir(path):
            return LIB_PHOTOS_NEW
    except OSError:
        pass
    return None


def default_search_roots() -> list[str]:
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    roots = [f"/media/{user}", f"/run/media/{user}", "/mnt", os.path.expanduser("~/Images"),
             os.path.expanduser("~/Pictures"), os.path.expanduser("~")]
    return [r for r in roots if os.path.isdir(r)]


def find_libraries(roots: Optional[list[str]] = None, max_depth: int = 3, time_limit: float = 20.0,
                   cancel: Optional[threading.Event] = None) -> list[tuple[str, str]]:
    """Recherche les bibliothèques iPhoto (et Photos) sous les dossiers donnés.

    Parcours en largeur, limité en profondeur et en durée ; ne descend jamais à
    l'intérieur d'une bibliothèque trouvée. Lecture seule.
    """
    roots = roots if roots is not None else default_search_roots()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    deadline = time.time() + time_limit
    skip = {"$RECYCLE.BIN", "System Volume Information", "lost+found", "node_modules", "__pycache__",
            "Masters", "Previews", "Thumbnails", "Originals", "Modified"}
    frontier = [(os.path.abspath(r), 0) for r in roots]
    while frontier:
        if time.time() > deadline or (cancel is not None and cancel.is_set()):
            break
        path, depth = frontier.pop(0)
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        kind = library_kind(path)
        if kind:
            found.append((path, kind))
            continue
        if depth >= max_depth:
            continue
        try:
            with os.scandir(path) as it:
                entries = sorted((e for e in it if not e.name.startswith(".") and e.name not in skip),
                                 key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    frontier.append((e.path, depth + 1))
            except OSError:
                pass
    return found


def library_status(root: str) -> str:
    """Texte court décrivant l'état d'une bibliothèque (pour la liste de choix)."""
    kind = library_kind(root)
    if kind is None:
        return "inaccessible" if not os.path.exists(root) else "pas de AlbumData.xml"
    if kind == LIB_PHOTOS_NEW:
        return "Photos 5+ : non pris en charge"
    prefix = "Photos · " if kind == LIB_PHOTOS else ""
    cache = os.path.join(library_cache_dir(root), "index.sqlite3")
    if os.path.isfile(cache):
        try:
            con = sqlite3.connect(f"file:{cache}?mode=ro", uri=True)
            try:
                n = dict(con.execute("SELECT key, value FROM meta").fetchall()).get("photo_count")
            finally:
                con.close()
            if n:
                return f"{prefix}indexée · {plural(int(n), 'photo')}"
        except (sqlite3.Error, ValueError):
            pass
    return f"{prefix}non indexée"


# ---------------------------------------------------------------------------
# AlbumManager : albums, dossiers et événements
# ---------------------------------------------------------------------------

class AlbumManager:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._albums: dict[int, Album] = {}
        self.reload()

    def reload(self) -> None:
        self._albums = {}
        for r in self.con.execute("SELECT * FROM albums ORDER BY position"):
            self._albums[r["id"]] = Album(
                id=r["id"], name=r["name"], kind=r["kind"], raw_type=r["raw_type"],
                parent_id=r["parent_id"], position=r["position"],
                photo_count=r["photo_count"] or 0, date=r["date"], roll_id=r["roll_id"],
                guid=r["guid"] or "", year=r["year"], first_date=r["first_date"],
            )
        # Protection contre d'éventuels cycles de parenté : on coupe le lien.
        for a in self._albums.values():
            seen = {a.id}
            p = a.parent_id
            while p is not None:
                if p in seen or p not in self._albums:
                    a.parent_id = None
                    break
                seen.add(p)
                p = self._albums[p].parent_id

    def get(self, album_id: int) -> Optional[Album]:
        return self._albums.get(album_id)

    def all(self) -> list[Album]:
        return list(self._albums.values())

    def children(self, parent_id: Optional[int]) -> list[Album]:
        return sorted((a for a in self._albums.values() if a.parent_id == parent_id),
                      key=lambda a: a.position)

    def path(self, album: Album) -> list[Album]:
        """Chaîne des dossiers parents (du plus haut au plus proche)."""
        chain = []
        p = album.parent_id
        while p is not None and p in self._albums:
            chain.append(self._albums[p])
            p = self._albums[p].parent_id
        return list(reversed(chain))

    def path_label(self, album: Album) -> str:
        return " › ".join(a.name for a in self.path(album))

    def top_folders(self) -> list[Album]:
        return [a for a in self.children(None) if a.kind == KIND_FOLDER]

    def albums_flat(self) -> list[Album]:
        """Tous les albums consultables (hors dossiers et événements), triés par nom."""
        items = [a for a in self._albums.values() if a.kind in (KIND_ALBUM, KIND_SMART, KIND_SPECIAL)]
        return sorted(items, key=lambda a: (normalize_text(a.name), normalize_text(self.path_label(a)),
                                            a.first_date or 0, a.id))

    def events_by_year(self) -> list[tuple[Optional[int], list[Album]]]:
        events = [a for a in self._albums.values() if a.kind == KIND_EVENT]
        groups: dict[Optional[int], list[Album]] = {}
        for e in events:
            groups.setdefault(e.year, []).append(e)
        result = []
        for year in sorted(groups, key=lambda y: (y is None, -(y or 0))):
            evs = sorted(groups[year], key=lambda e: (e.date if e.date is not None else 0, e.id))
            result.append((year, evs))
        return result

    def search(self, query: str, limit: int = 300) -> list[Album]:
        q = normalize_text(query.strip())
        if not q:
            return []
        rows = self.con.execute(
            "SELECT id FROM albums WHERE name_norm LIKE ? ESCAPE '\\' AND kind != ? "
            "ORDER BY kind, name_norm LIMIT ?",
            ("%" + _like_escape(q) + "%", KIND_ALL, limit),
        ).fetchall()
        return [self._albums[r[0]] for r in rows if r[0] in self._albums]


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------------
# PhotoManager : accès aux photos et à leurs métadonnées
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class View:
    """Ce qui est affiché dans la grille."""
    kind: str                     # "all" | "album" | "year" | "search" | "keys" | "folder" | "empty"
    album_id: Optional[int] = None
    year: Optional[int] = None
    query: str = ""
    title: str = ""
    keys: tuple = ()              # "keys" : favoris ou tag (GUID, ou « id:N »)
    ids: tuple = ()               # "ids" : résultat d'une recherche avancée, groupe de doublons…
    start: Optional[float] = None # "date" : intervalle [start, end[ (secondes Apple)
    end: Optional[float] = None
    level: str = ""               # "timeline" : years | months | days
    month: Optional[int] = None


class PhotoManager:
    COLUMNS = ("p.id, p.guid, p.caption, p.comment, p.rating, p.roll_id, p.media_type, "
               "p.image_path, p.original_path, p.thumb_path, p.date, p.mod_date, p.filename, "
               "p.event_name")

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    @staticmethod
    def _photo(r) -> Photo:
        return Photo(
            id=r["id"], guid=r["guid"] or "", caption=r["caption"] or "",
            comment=r["comment"] or "", rating=r["rating"] or 0, roll_id=r["roll_id"],
            media_type=r["media_type"] or "Image", image_path=r["image_path"] or "",
            original_path=r["original_path"] or "", thumb_path=r["thumb_path"] or "",
            date=r["date"], mod_date=r["mod_date"], filename=r["filename"] or "",
            event_name=r["event_name"] or "",
        )

    def _sql(self, view: View, select: str) -> tuple[str, list]:
        """Renvoie (requête, paramètres) pour la vue, avec ORDER BY."""
        if view.kind == "all":
            return (f"SELECT {select} FROM photos p "
                    "ORDER BY p.date, p.id", [])
        if view.kind == "album":
            return (f"SELECT {select} FROM album_photos ap JOIN photos p ON p.id = ap.photo_id "
                    f"WHERE ap.album_id = ? ORDER BY ap.position", [view.album_id])
        if view.kind == "year":
            cond = "e.year IS NULL" if view.year is None else "e.year = ?"
            params = [] if view.year is None else [view.year]
            return (f"SELECT {select} FROM albums e JOIN album_photos ap ON ap.album_id = e.id "
                    f"JOIN photos p ON p.id = ap.photo_id "
                    f"WHERE e.kind = 'event' AND {cond} ORDER BY e.date, e.id, ap.position", params)
        if view.kind == "search":
            q = "%" + _like_escape(normalize_text(view.query.strip())) + "%"
            if view.album_id is not None:
                return (f"SELECT {select} FROM album_photos ap JOIN photos p ON p.id = ap.photo_id "
                        f"WHERE ap.album_id = ? AND p.search LIKE ? ESCAPE '\\' "
                        "ORDER BY ap.position", [view.album_id, q])
            return (f"SELECT {select} FROM photos p "
                    "WHERE p.search LIKE ? ESCAPE '\\' ORDER BY p.date, p.id", [q])
        if view.kind == "ids":
            return (f"SELECT {select} FROM photos p WHERE p.id IN (SELECT value FROM json_each(?)) "
                    "ORDER BY p.date, p.id", [json.dumps(list(view.ids))])
        if view.kind == "date":
            return (f"SELECT {select} FROM photos p WHERE p.date >= ? AND p.date < ? ORDER BY p.date, p.id",
                    [view.start, view.end])
        if view.kind == "keys":
            keys = json.dumps(list(view.keys))
            return (f"SELECT {select} FROM photos p "
                    "WHERE p.guid IN (SELECT value FROM json_each(?)) "
                    "OR ('id:' || p.id) IN (SELECT value FROM json_each(?)) ORDER BY p.date, p.id",
                    [keys, keys])
        return ("SELECT " + select + " FROM photos p WHERE 0", [])

    def count(self, view: View) -> int:
        sql, params = self._sql(view, "p.id")
        return self.con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]

    def page(self, view: View, offset: int, limit: int = PAGE_SIZE) -> list[Photo]:
        sql, params = self._sql(view, self.COLUMNS)
        rows = self.con.execute(f"{sql} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
        return [self._photo(r) for r in rows]

    def ids(self, view: View) -> list[int]:
        sql, params = self._sql(view, "p.id")
        return [r[0] for r in self.con.execute(sql, params)]

    def get(self, photo_id: int) -> Optional[Photo]:
        r = self.con.execute(f"SELECT {self.COLUMNS} FROM photos p WHERE p.id = ?",
                             (photo_id,)).fetchone()
        return self._photo(r) if r else None

    def albums_of(self, photo_id: int) -> list[int]:
        return [r[0] for r in self.con.execute(
            "SELECT album_id FROM album_photos WHERE photo_id = ?", (photo_id,))]


# ---------------------------------------------------------------------------
# Chargement des images (threads)
# ---------------------------------------------------------------------------

_HEIF_STATE: dict = {}


def heif_supported() -> bool:
    """Le décodage HEIC est-il possible (GdkPixbuf + chargeur heif) ?"""
    if "ok" not in _HEIF_STATE:
        ok = False
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
            names = {f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()}
            ok = any("heif" in n for n in names)
            _HEIF_STATE["GdkPixbuf"] = GdkPixbuf
        except Exception:
            ok = False
        _HEIF_STATE["ok"] = ok
    return _HEIF_STATE["ok"]


def is_displayable(path: str) -> bool:
    ext = file_ext(path)
    return ext in IMAGE_EXTS or ext in RAW_TIFF_EXTS or (ext in HEIF_EXTS and heif_supported())


def _load_heif(path: str, max_w: int, max_h: int):
    GdkPixbuf = _HEIF_STATE["GdkPixbuf"]
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_w, max_h, True)   # lecture seule
    pb = pb.apply_embedded_orientation() or pb
    mode = "RGBA" if pb.get_has_alpha() else "RGB"
    img = Image.frombuffer(mode, (pb.get_width(), pb.get_height()), pb.get_pixels(),
                           "raw", mode, pb.get_rowstride(), 1)
    return img.copy()


def _jpeg_sof(prefix: bytes) -> Optional[int]:
    """Renvoie le marqueur SOF (0xC0…0xCF) d'un JPEG, d'après ses premiers octets."""
    i = 2
    while i + 4 <= len(prefix):
        if prefix[i] != 0xFF:
            return None
        marker = prefix[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return marker
        length = struct.unpack(">H", prefix[i + 2:i + 4])[0]
        i += 2 + length
    return None


def extract_raw_preview(path: str) -> tuple[Optional[bytes], int]:
    """Extrait le plus grand aperçu JPEG lisible d'un RAW au format TIFF (CR2, NEF, DNG…).

    Renvoie (données JPEG, orientation EXIF). Le fichier n'est lu qu'en partie.
    """
    orientation = 1
    with open(path, "rb") as fh:
        head = fh.read(8)
        if head[:2] == b"II":
            e = "<"
        elif head[:2] == b"MM":
            e = ">"
        else:
            return None, 1
        size = os.fstat(fh.fileno()).st_size
        first = struct.unpack(e + "I", head[4:8])[0]
        type_sizes = {1: 1, 3: 2, 4: 4, 7: 1, 9: 4, 13: 4}
        candidates: list[tuple[int, int]] = []     # (taille, position)
        queue_ifd = [(first, True)]
        visited: set[int] = set()

        def values(typ, count, raw):
            unit = type_sizes.get(typ)
            if unit is None:
                return []
            fmt = {1: "B", 3: "H", 4: "I", 7: "B", 9: "i", 13: "I"}[typ]
            if unit * count <= 4:
                data = raw[:unit * count]
            else:
                if count > 4096:
                    return []
                fh.seek(struct.unpack(e + "I", raw)[0])
                data = fh.read(unit * count)
            n = len(data) // unit
            return list(struct.unpack(e + fmt * n, data[:n * unit]))

        while queue_ifd and len(visited) < 24:
            off, is_first = queue_ifd.pop(0)
            if off <= 0 or off >= size or off in visited:
                continue
            visited.add(off)
            fh.seek(off)
            raw_n = fh.read(2)
            if len(raw_n) < 2:
                continue
            n = struct.unpack(e + "H", raw_n)[0]
            if n == 0 or n > 512:
                continue
            block = fh.read(12 * n + 4)
            if len(block) < 12 * n + 4:
                continue
            tags = {}
            for k in range(n):
                tag, typ, count = struct.unpack(e + "HHI", block[12 * k:12 * k + 8])
                tags[tag] = (typ, count, block[12 * k + 8:12 * k + 12])
            nxt = struct.unpack(e + "I", block[12 * n:12 * n + 4])[0]
            get = lambda t: values(*tags[t]) if t in tags else []
            if is_first and 0x0112 in tags:
                o = get(0x0112)
                if o:
                    orientation = o[0]
            if 0x0201 in tags and 0x0202 in tags:
                a, b = get(0x0201), get(0x0202)
                if a and b:
                    candidates.append((b[0], a[0]))
            comp = get(0x0103)
            if 0x0111 in tags and 0x0117 in tags and (not comp or comp[0] in (6, 7)):
                offs, counts = get(0x0111), get(0x0117)
                if len(offs) == 1 and len(counts) == 1:
                    candidates.append((counts[0], offs[0]))
            for sub in get(0x014A):
                queue_ifd.append((sub, False))
            if nxt:
                queue_ifd.append((nxt, False))

        for length, pos in sorted(set(candidates), reverse=True):
            if length < 1024 or pos + length > size:
                continue
            fh.seek(pos)
            prefix = fh.read(min(length, 65536))
            if prefix[:2] != b"\xff\xd8":
                continue
            sof = _jpeg_sof(prefix)
            if sof not in (0xC0, 0xC1, 0xC2):
                continue   # JPEG sans perte (données RAW) : non décodable
            fh.seek(pos)
            return fh.read(length), orientation
    return None, orientation


_ORIENT_OPS = {2: [0], 3: [3], 4: [1], 5: [0, 4], 6: [4], 7: [0, 2], 8: [2]}


def _apply_orientation(img, orientation: int):
    """Applique une orientation EXIF (1…8) à une image PIL."""
    ops = {0: Image.FLIP_LEFT_RIGHT, 1: Image.FLIP_TOP_BOTTOM, 2: Image.ROTATE_90,
           3: Image.ROTATE_180, 4: Image.ROTATE_270}
    for op in _ORIENT_OPS.get(orientation, []):
        img = img.transpose(ops[op])
    return img


def _load_rawpy(path: str, max_w: int, max_h: int):
    """Développement d'un RAW sans aperçu intégré (module facultatif rawpy, lecture seule)."""
    with rawpy.imread(path) as raw:
        rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False, output_bps=8)
    img = Image.fromarray(rgb)
    img.thumbnail((max_w, max_h))
    return img


def load_pil_image(path: str, max_w: int, max_h: int):
    """Ouvre une image en lecture seule et la réduit (sans déformation).

    Gère aussi le HEIC (via GdkPixbuf) et les RAW TIFF (aperçu JPEG intégré).
    """
    ext = file_ext(path)
    if ext in HEIF_EXTS and heif_supported():
        img = _load_heif(path, max_w, max_h)
    elif ext in HEIF_EXTS and not PILLOW_HEIF:
        raise ValueError("format HEIC non pris en charge : installez heif-gdk-pixbuf (ou python3-pillow-heif)")
    else:
        raw_orientation = None
        data = None
        if ext in RAW_TIFF_EXTS:
            data, raw_orientation = extract_raw_preview(path)
        if ext in RAW_EXTS and data is None:
            if rawpy is None:
                raise ValueError("aucun aperçu JPEG intégré dans ce fichier RAW "
                                 "(installez le module facultatif rawpy pour le développer)")
            return _load_rawpy(path, max_w, max_h)
        if data is not None:
            fh = io.BytesIO(data)
        else:
            with open(path, "rb") as f:
                fh = io.BytesIO(f.read())
        img = Image.open(fh)
        try:
            img.draft("RGB", (max_w, max_h))   # décodage JPEG réduit : très rapide
        except Exception:
            pass
        img.load()
        try:
            if raw_orientation is not None:
                img = _apply_orientation(img, raw_orientation)
            else:
                img = ImageOps.exif_transpose(img)
        except Exception:
            pass
    if img.mode not in ("RGB", "L"):
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (40, 40, 40))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    img.thumbnail((max_w, max_h), Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC)
    return img


class ThumbnailLoader:
    """Charge les vignettes en arrière-plan (disque -> cache local -> PIL)."""

    def __init__(self, library: LibraryManager, cache: CacheManager):
        self.library = library
        self.cache = cache
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=THUMB_WORKERS,
                                                          thread_name_prefix="vignettes")
        self.results: "queue.Queue[tuple]" = queue.Queue()
        self.futures: list[concurrent.futures.Future] = []

    def cancel_pending(self) -> None:
        for f in self.futures:
            f.cancel()
        self.futures = []

    def request(self, generation: int, index: int, photo: Photo) -> None:
        self.futures.append(self.pool.submit(self._work, generation, index, photo))

    def _work(self, generation: int, index: int, photo: Photo) -> None:
        img = None
        status = ""
        exists = self.library.original_exists(photo)
        cache_path = self.cache.thumb_cache_path(photo)
        try:
            if os.path.isfile(cache_path):
                img = load_pil_image(cache_path, THUMB_SIZE, THUMB_SIZE)
            else:
                src, _origin = self.library.find_display_file(photo, for_thumbnail=True)
                if src:
                    big = load_pil_image(src, THUMB_CACHE_SIZE, THUMB_CACHE_SIZE)
                    try:
                        self.cache.library.assert_outside(cache_path)
                        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                        tmp = cache_path + ".part"
                        big.save(tmp, "JPEG", quality=85)
                        os.replace(tmp, cache_path)
                    except OSError:
                        pass  # le cache est facultatif
                    big.thumbnail((THUMB_SIZE, THUMB_SIZE))
                    img = big
                elif photo.is_video:
                    status = "video"
                elif file_ext(photo.image_path) in RAW_EXTS:
                    status = "raw"
                else:
                    status = "missing"
        except Exception as exc:  # fichier illisible : on n'interrompt rien
            status = "error"
            img = None
            sys.stderr.write(f"[vignette] {photo.id} : {exc}\n")
        self.results.put((generation, index, photo.id, img, exists, status))

    def shutdown(self) -> None:
        self.cancel_pending()
        self.pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Exploration de dossiers indépendants (lecture seule)
# ---------------------------------------------------------------------------

FOLDER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
                     # 2.0 : RAW (aperçu intégré, ou rawpy) et HEIC (GdkPixbuf ou pillow-heif) ;
                     # un format non décodable est signalé sur sa vignette.
                     ".cr2", ".nef", ".arw", ".dng", ".pef", ".srw", ".heic", ".heif"}
FOLDER_CACHE_DIR = os.path.join(CACHE_DIR, "dossiers")

# (libellé, clé, ordre décroissant)
FOLDER_SORTS = [
    ("Nom (A → Z)", "name", False),
    ("Nom (Z → A)", "name", True),
    ("Date de modification (récentes)", "mtime", True),
    ("Date de modification (anciennes)", "mtime", False),
    ("Taille (grandes d'abord)", "size", True),
    ("Taille (petites d'abord)", "size", False),
]


def safe_text(value: str) -> str:
    """Nom affichable dans Tk, même si le nom de fichier n'est pas de l'UTF-8 valide."""
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def unix_to_interval(ts: float) -> Optional[float]:
    """Date Unix -> « TimerInterval » Apple (heure locale), pour réutiliser format_date."""
    try:
        return (_dt.datetime.fromtimestamp(ts) - APPLE_EPOCH).total_seconds()
    except (OverflowError, OSError, ValueError):
        return None


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} o"
    for unit in ("Ko", "Mo", "Go", "To"):
        n /= 1024
        if n < 1024 or unit == "To":
            return f"{n:.1f} {unit}".replace(".", ",")
    return ""


def natural_key(text: str) -> list:
    """Clé de tri « naturelle » : IMG_2 avant IMG_10, sans tenir compte de la casse ni des accents."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", normalize_text(text))]


def image_size(path: str) -> Optional[tuple[int, int]]:
    """Dimensions d'une image (lecture de l'en-tête uniquement), orientation EXIF comprise."""
    if Image is None or file_ext(path) in RAW_TIFF_EXTS | HEIF_EXTS:
        return None
    try:
        with Image.open(path) as im:
            w, h = im.size
            try:
                if im.getexif().get(0x0112) in (5, 6, 7, 8):
                    w, h = h, w
            except Exception:
                pass
            return w, h
    except Exception:
        return None


class FolderScanner:
    """Recherche les images d'un dossier (et éventuellement de ses sous-dossiers).

    Lecture seule : seuls os.scandir() et stat() sont utilisés. Les dossiers
    inaccessibles et les fichiers illisibles sont comptés, sans interrompre l'analyse.
    Les éléments cachés (« .xxx », dont les fichiers « ._xxx » des disques macOS)
    et les liens symboliques vers des dossiers sont ignorés.
    """

    def __init__(self, root: str, recursive: bool):
        self.root = root
        self.recursive = recursive
        self.cancelled = threading.Event()
        self.entries: list[tuple[str, str, str, float, int]] = []   # chemin, sous-dossier, nom, mtime, taille
        self.errors: list[tuple[str, str]] = []                     # dossiers inaccessibles
        self.unreadable = 0
        self.dirs_seen = 0

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self, progress: Callable[[int, int], None] = lambda d, n: None) -> None:
        stack: list[tuple[str, str]] = [(self.root, "")]
        last = time.monotonic()
        while stack:
            if self.cancelled.is_set():
                return
            directory, rel = stack.pop()
            try:
                with os.scandir(directory) as it:
                    items = list(it)
            except OSError as exc:
                self.errors.append((rel or ".", exc.strerror or str(exc)))
                continue
            self.dirs_seen += 1
            subdirs = []
            for e in items:
                name = e.name
                if name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if self.recursive:
                            subdirs.append((e.path, f"{rel}/{name}" if rel else name))
                        continue
                    if file_ext(name) not in FOLDER_IMAGE_EXTS or not e.is_file():
                        continue
                    st = e.stat()
                except OSError:
                    self.unreadable += 1
                    continue
                self.entries.append((e.path, rel, name, st.st_mtime, st.st_size))
            subdirs.sort(key=lambda d: natural_key(d[1]), reverse=True)
            stack.extend(subdirs)
            now = time.monotonic()
            if now - last > 0.25:
                last = now
                progress(self.dirs_seen, len(self.entries))


class FolderBrowser:
    """Contenu d'un dossier analysé : filtre, tri et pagination.

    Expose la même interface que PhotoManager (count, page, ids, get), ce qui permet
    de réutiliser la grille de vignettes et la visionneuse.
    """

    def __init__(self):
        self.root = ""
        self.recursive = False
        self.photos: list[Photo] = []
        self.errors: list[tuple[str, str]] = []
        self.unreadable = 0
        self.dir_counts: dict[str, int] = {}
        self.sort_key = "name"
        self.sort_desc = False
        self.query = ""
        self.subdir: Optional[str] = None
        self.only: Optional[set[str]] = None     # favoris / tag : clés des images à garder
        self.only_title = ""
        self.date_range: Optional[tuple[float, float]] = None   # chronologie (secondes Apple)
        self.order: list[int] = []

    def load(self, scanner: FolderScanner) -> None:
        self.root = scanner.root
        self.recursive = scanner.recursive
        self.errors = scanner.errors
        self.unreadable = scanner.unreadable
        self.photos = []
        self.dir_counts = {}
        for i, (path, rel, name, mtime, size) in enumerate(scanner.entries):
            interval = unix_to_interval(mtime)
            self.photos.append(Photo(
                id=i, guid="", caption="", comment="", rating=0, roll_id=None, media_type="Image",
                image_path=path, original_path="", thumb_path="", date=interval, mod_date=mtime,
                filename=safe_text(name), size=size, folder=safe_text(rel), source="folder"))
            key = safe_text(rel)
            self.dir_counts[key] = self.dir_counts.get(key, 0) + 1
        self.query = ""
        self.subdir = None
        self.only = None
        self.date_range = None
        self.refresh()

    def set_sort(self, key: str, desc: bool) -> None:
        self.sort_key, self.sort_desc = key, desc
        self.refresh()

    def refresh(self) -> None:
        """Recalcule la liste affichée (sous-dossier, recherche, tri)."""
        idx = range(len(self.photos))
        sub = self.subdir
        if sub:
            prefix = sub + "/"
            idx = [i for i in idx if self.photos[i].folder == sub or self.photos[i].folder.startswith(prefix)]
        if self.only is not None:
            only = self.only
            idx = [i for i in idx if path_key(self.photos[i].image_path) in only]
        if self.date_range is not None:
            a, b = self.date_range
            idx = [i for i in idx if self.photos[i].date is not None and a <= self.photos[i].date < b]
        q = normalize_text(self.query.strip())
        if q:
            idx = [i for i in idx if q in normalize_text(self.photos[i].filename)]
        ph = self.photos
        if self.sort_key == "mtime":
            key = lambda i: (ph[i].mod_date or 0)
        elif self.sort_key == "size":
            key = lambda i: ph[i].size
        else:
            key = lambda i: natural_key(f"{ph[i].folder}/{ph[i].filename}" if ph[i].folder else ph[i].filename)
        if self.sort_key == "position":
            key = lambda i: i
        self.order = sorted(idx, key=key, reverse=self.sort_desc)

    # -- interface commune avec PhotoManager -----------------------------------
    def count(self, _view=None) -> int:
        return len(self.order)

    def page(self, _view, offset: int, limit: int = PAGE_SIZE) -> list[Photo]:
        return [self.photos[i] for i in self.order[offset:offset + limit]]

    def ids(self, _view=None) -> list[int]:
        return list(self.order)

    def get(self, photo_id: int) -> Optional[Photo]:
        return self.photos[photo_id] if 0 <= photo_id < len(self.photos) else None


class FolderLibrary:
    """Équivalent de LibraryManager pour un dossier indépendant (lecture seule)."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root) if root else ""

    existing = staticmethod(LibraryManager.existing)

    def is_available(self) -> bool:
        return bool(self.root) and os.path.isdir(self.root)

    def unavailable_message(self) -> str:
        return (f"Le dossier est inaccessible :\n{self.root}\n\n"
                "Vérifiez que le disque qui le contient est bien branché et monté.")

    def to_linux(self, path: str) -> str:
        return path

    def original_exists(self, photo: Photo) -> bool:
        try:
            return os.path.isfile(photo.image_path)
        except (OSError, ValueError):
            return False

    def find_display_file(self, photo: Photo, for_thumbnail: bool) -> tuple[Optional[str], str]:
        return (photo.image_path, "image") if self.original_exists(photo) else (None, "")

    def assert_outside(self, path: str) -> None:
        """Garde-fou : refuse toute écriture dans le dossier consulté."""
        if self.root and is_inside(path, self.root):
            raise PermissionError(f"Écriture refusée : {path} se trouve dans le dossier consulté.")


class CollectionBrowser(FolderBrowser):
    """Contenu d'un album virtuel : références vers des fichiers de plusieurs sources (ordre de l'album)."""

    def load_items(self, items: list) -> None:
        self.root = ""
        self.photos = []
        self.missing = 0
        for i, it in enumerate(items):
            try:
                st = os.stat(it.path)
                size, mtime = st.st_size, st.st_mtime
            except (OSError, ValueError):
                size, mtime = it.size, 0.0
                self.missing += 1
            self.photos.append(Photo(
                id=i, guid="", caption="", comment="", rating=0, roll_id=None, media_type="Image",
                image_path=it.path, original_path="", thumb_path="", date=it.date, mod_date=mtime,
                filename=it.filename or os.path.basename(it.path), size=size, folder="", source="folder",
                ref=(it.scope, it.key)))
        self.dir_counts = {}
        self.query = ""
        self.subdir = None
        self.only = None
        self.date_range = None
        self.sort_key, self.sort_desc = "position", False
        self.refresh()


class CollectionLibrary(FolderLibrary):
    """Équivalent de FolderLibrary pour les albums virtuels : chemins absolus, sources protégées."""

    def __init__(self, protected: Callable[[], list]):
        super().__init__("")
        self._protected = protected
        self._dirs: set[str] = set()

    def set_items(self, items: list) -> None:
        self._dirs = {os.path.dirname(i.path) for i in items if i.path}

    def is_available(self) -> bool:
        return True

    def unavailable_message(self) -> str:
        return "Certaines photos de cet album sont introuvables (disque débranché ou fichier déplacé)."

    def assert_outside(self, path: str) -> None:
        storage.assert_outside(path, list(self._protected()) + sorted(self._dirs))


class FolderThumbCache:
    """Cache des miniatures des dossiers indépendants (~/.cache/photoexplorer_iphoto/dossiers/)."""

    def __init__(self, library: FolderLibrary):
        self.library = library
        self.cache_dir = FOLDER_CACHE_DIR
        self.thumb_dir = os.path.join(self.cache_dir, "thumbs")

    def ensure_dirs(self) -> None:
        self.library.assert_outside(self.thumb_dir)
        os.makedirs(self.thumb_dir, exist_ok=True)

    def usable(self) -> bool:
        """Le cache est-il hors du dossier consulté ?"""
        return not (self.library.root and is_inside(self.thumb_dir, self.library.root))

    def thumb_cache_path(self, photo: Photo) -> str:
        key = f"{photo.image_path}|{photo.size}|{photo.mod_date}"
        digest = hashlib.sha1(key.encode("utf-8", "surrogateescape")).hexdigest()
        return os.path.join(self.thumb_dir, digest[:2], digest + ".jpg")

    clear_thumbnails = CacheManager.clear_thumbnails
    thumbnails_size = CacheManager.thumbnails_size


# ---------------------------------------------------------------------------
# Favoris et tags (stockés hors des bibliothèques et des dossiers consultés)
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "iphotor")
ANNOTATIONS_PATH = os.path.join(DATA_DIR, "annotations.sqlite3")
FILES_SCOPE = "fichiers"   # images des dossiers indépendants (clé = chemin absolu)


def library_scope(root: str) -> str:
    return "bib:" + os.path.abspath(root.rstrip("/"))


def path_key(path: str) -> str:
    """Clé texte d'un chemin, même si le nom n'est pas de l'UTF-8 valide."""
    path = os.path.abspath(path)
    try:
        path.encode("utf-8")
        return path
    except UnicodeEncodeError:
        return "hex:" + os.fsencode(path).hex()


def photo_scope_key(photo: Photo, library_root: str) -> tuple[str, str]:
    """(portée, clé) qui identifie durablement une photo pour les favoris et les tags."""
    if photo.source == "folder":
        return FILES_SCOPE, path_key(photo.image_path)
    return library_scope(library_root), (photo.guid or f"id:{photo.id}")


def clean_tag(tag: str) -> str:
    return " ".join(tag.replace(",", " ").split())[:60]


class Annotations:
    """Favoris et tags de l'utilisateur (~/.local/share/iphotor/annotations.sqlite3).

    Ces informations appartiennent à iphotor : elles ne sont jamais écrites dans la
    bibliothèque ni dans les dossiers consultés, et survivent à un vidage du cache.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS favorites (scope TEXT, key TEXT, added REAL, PRIMARY KEY (scope, key));
    CREATE TABLE IF NOT EXISTS tags (scope TEXT, key TEXT, tag TEXT, PRIMARY KEY (scope, key, tag));
    CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(scope, tag);
    """

    def __init__(self, path: str = ANNOTATIONS_PATH):
        self.path = path
        self.con: Optional[sqlite3.Connection] = None
        self.error = ""
        self._fav: dict[str, set[str]] = {}
        self._tags: dict[str, dict[str, list[str]]] = {}

    def _db(self) -> sqlite3.Connection:
        if self.con is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.con = sqlite3.connect(self.path)
            self.con.executescript(self.SCHEMA)
        return self.con

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
            self.con = None

    def _load(self, scope: str) -> None:
        if scope in self._fav:
            return
        con = self._db()
        self._fav[scope] = {r[0] for r in con.execute("SELECT key FROM favorites WHERE scope=?", (scope,))}
        tags: dict[str, list[str]] = {}
        for key, tag in con.execute("SELECT key, tag FROM tags WHERE scope=?", (scope,)):
            tags.setdefault(key, []).append(tag)
        for lst in tags.values():
            lst.sort(key=normalize_text)
        self._tags[scope] = tags

    # -- favoris -------------------------------------------------------------
    def is_favorite(self, scope: str, key: str) -> bool:
        self._load(scope)
        return key in self._fav[scope]

    def set_favorite(self, scope: str, key: str, on: bool) -> None:
        self._load(scope)
        con = self._db()
        with con:
            if on:
                con.execute("INSERT OR REPLACE INTO favorites VALUES (?, ?, ?)", (scope, key, time.time()))
                self._fav[scope].add(key)
            else:
                con.execute("DELETE FROM favorites WHERE scope=? AND key=?", (scope, key))
                self._fav[scope].discard(key)

    def favorites(self, scope: str) -> set[str]:
        self._load(scope)
        return set(self._fav[scope])

    # -- tags ----------------------------------------------------------------
    def tags_of(self, scope: str, key: str) -> list[str]:
        self._load(scope)
        return list(self._tags[scope].get(key, []))

    def set_tags(self, scope: str, key: str, tags: Iterable[str]) -> None:
        self._load(scope)
        clean = sorted({t for t in (clean_tag(x) for x in tags) if t}, key=normalize_text)
        con = self._db()
        with con:
            con.execute("DELETE FROM tags WHERE scope=? AND key=?", (scope, key))
            con.executemany("INSERT INTO tags VALUES (?, ?, ?)", [(scope, key, t) for t in clean])
        if clean:
            self._tags[scope][key] = clean
        else:
            self._tags[scope].pop(key, None)

    def all_tags(self, scope: str) -> list[str]:
        self._load(scope)
        names = {t for lst in self._tags[scope].values() for t in lst}
        return sorted(names, key=normalize_text)

    def keys_with_tag(self, scope: str, tag: str) -> set[str]:
        self._load(scope)
        return {k for k, lst in self._tags[scope].items() if tag in lst}


# ---------------------------------------------------------------------------
# Informations EXIF
# ---------------------------------------------------------------------------

_EXIF_ORIENT = {1: "Normale", 2: "Miroir horizontal", 3: "Retournée (180°)", 4: "Miroir vertical",
                5: "Miroir + rotation 90° antihoraire", 6: "Rotation 90° horaire",
                7: "Miroir + rotation 90° horaire", 8: "Rotation 90° antihoraire"}
_EXIF_PROGRAM = {0: "Non défini", 1: "Manuel", 2: "Programme normal", 3: "Priorité ouverture",
                 4: "Priorité vitesse", 5: "Créatif", 6: "Action", 7: "Portrait", 8: "Paysage"}
_EXIF_METERING = {0: "Inconnue", 1: "Moyenne", 2: "Pondérée centrale", 3: "Spot", 4: "Multi-spot",
                  5: "Matricielle", 6: "Partielle", 255: "Autre"}
_EXIF_WB = {0: "Automatique", 1: "Manuelle"}
_EXIF_SKIP_BINARY = {0x927C, 0x9286, 0x02BC, 0x8773, 0x83BB}  # MakerNote, UserComment, XMP, ICC, IPTC


def _rational(value) -> Optional[float]:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return value[0] / value[1] if value[1] else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _exif_text(value) -> str:
    """Valeur EXIF brute -> texte lisible."""
    if isinstance(value, bytes):
        raw = value.rstrip(b"\x00")
        if raw and len(raw) <= 200 and all(32 <= b < 127 for b in raw):
            return raw.decode("ascii")
        return f"(données binaires, {len(value)} octets)"
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    if isinstance(value, tuple):
        return ", ".join(_exif_text(v) for v in value[:16]) + (" …" if len(value) > 16 else "")
    f = _rational(value) if not isinstance(value, int) else None
    if f is not None and not isinstance(value, (int, float)):
        return f"{f:.6g}"
    return str(value)


def _gps_degrees(dms, ref) -> Optional[float]:
    try:
        d, m, s = (_rational(x) for x in dms)
        val = d + m / 60 + s / 3600
    except (TypeError, ValueError):
        return None
    ref = ref.decode() if isinstance(ref, bytes) else str(ref or "")
    return -val if ref.strip().upper() in ("S", "W") else val


def _user_comment(value) -> str:
    if isinstance(value, bytes) and len(value) > 8:
        head, body = value[:8], value[8:]
        try:
            text = body.decode("utf-16" if head.startswith(b"UNICODE") else "latin-1")
        except UnicodeDecodeError:
            return ""
        return text.replace("\x00", "").strip()
    return _exif_text(value) if isinstance(value, str) else ""


def read_exif(path: str) -> dict:
    """Lit les métadonnées EXIF d'un fichier (lecture seule, en-tête uniquement).

    Renvoie {"summary": [(libellé, valeur)], "groups": [(groupe, [(balise, valeur)])],
             "gps": (lat, lon) | None, "error": str}.
    """
    result: dict = {"summary": [], "groups": [], "gps": None, "error": ""}
    try:
        import warnings
        from PIL import ExifTags
        with warnings.catch_warnings(), Image.open(path) as im:
            warnings.simplefilter("ignore")
            fmt, dims, mode = im.format or "", im.size, im.mode
            exif = im.getexif()
            ifd0 = dict(exif)
            try:
                sub = dict(exif.get_ifd(0x8769))
            except Exception:
                sub = {}
            try:
                gps = dict(exif.get_ifd(0x8825))
            except Exception:
                gps = {}
            info = {k: v for k, v in im.info.items() if isinstance(v, str) and len(v) < 300}
    except Exception as exc:
        tool = shutil.which("exiftool")
        if tool:
            return _read_exif_tool(tool, path)
        if file_ext(path) in HEIF_EXTS:
            result["error"] = ("Les informations EXIF des fichiers HEIC ne peuvent être lues qu'avec "
                               "l'outil exiftool (paquet libimage-exiftool-perl).")
        else:
            detail = "format non reconnu" if "cannot identify image file" in str(exc) else str(exc)
            result["error"] = f"Lecture des informations impossible : {detail}"
        return result

    tags = dict(ifd0)
    tags.update(sub)
    s: list[tuple[str, str]] = []
    s.append(("Format", f"{fmt} — {mode}" if fmt else mode))
    w, h = dims
    if file_ext(path) in RAW_EXTS and tags.get(0xA002) and tags.get(0xA003):
        w, h = int(tags[0xA002]), int(tags[0xA003])    # le RAW lui-même, pas son aperçu
    if tags.get(0x0112) in (5, 6, 7, 8):
        w, h = h, w
    s.append(("Dimensions", f"{w} × {h} px ({w * h / 1e6:.1f} Mpx)".replace(".", ",")))
    make, model = _exif_text(tags.get(0x010F, "")).strip(), _exif_text(tags.get(0x0110, "")).strip()
    if model or make:
        s.append(("Appareil", model if make and model.lower().startswith(make.split()[0].lower())
                  else " ".join(x for x in (make, model) if x)))
    if 0xA434 in tags:
        s.append(("Objectif", _exif_text(tags[0xA434])))
    for tag, label in ((0x9003, "Date de prise de vue"), (0x0132, "Date de modification")):
        if tag in tags:
            s.append((label, _exif_text(tags[tag]).replace(":", "-", 2).replace(" ", " à ", 1)))
            break
    t = _rational(tags.get(0x829A))
    if t:
        s.append(("Temps de pose", f"1/{round(1 / t)} s" if t < 1 else f"{t:g} s"))
    f = _rational(tags.get(0x829D))
    if f:
        s.append(("Ouverture", f"f/{f:.1f}".replace(".", ",")))
    iso = tags.get(0x8827)
    if iso:
        s.append(("Sensibilité", f"ISO {iso[0] if isinstance(iso, tuple) else iso}"))
    fl = _rational(tags.get(0x920A))
    if fl:
        eq = tags.get(0xA405)
        s.append(("Focale", f"{fl:g} mm".replace(".", ",") + (f" (équiv. 24×36 : {eq} mm)" if eq else "")))
    bias = _rational(tags.get(0x9204))
    if bias is not None and 0x9204 in tags:
        s.append(("Correction d'exposition", f"{bias:+.1f} EV".replace(".", ",")))
    if 0x8822 in tags:
        s.append(("Mode d'exposition", _EXIF_PROGRAM.get(tags[0x8822], str(tags[0x8822]))))
    if 0x9207 in tags:
        s.append(("Mesure", _EXIF_METERING.get(tags[0x9207], str(tags[0x9207]))))
    if 0x9209 in tags:
        try:
            s.append(("Flash", "Déclenché" if int(tags[0x9209]) & 1 else "Non déclenché"))
        except (TypeError, ValueError):
            pass
    if 0xA403 in tags:
        s.append(("Balance des blancs", _EXIF_WB.get(tags[0xA403], str(tags[0xA403]))))
    if 0x0112 in tags:
        s.append(("Orientation", _EXIF_ORIENT.get(tags[0x0112], str(tags[0x0112]))))
    for tag, label in ((0x0131, "Logiciel"), (0x013B, "Auteur"), (0x8298, "Copyright"),
                       (0x010E, "Description")):
        if tags.get(tag):
            txt = _exif_text(tags[tag])
            if txt and not txt.startswith("(données"):
                s.append((label, txt))
    uc = _user_comment(tags.get(0x9286))
    if uc:
        s.append(("Commentaire", uc))
    if gps:
        lat = _gps_degrees(gps.get(2), gps.get(1)) if 2 in gps else None
        lon = _gps_degrees(gps.get(4), gps.get(3)) if 4 in gps else None
        if lat is not None and lon is not None:
            result["gps"] = (lat, lon)
            s.append(("Coordonnées GPS", f"{lat:.6f}, {lon:.6f}"))
        alt = _rational(gps.get(6))
        if alt is not None:
            below = gps.get(5) in (1, b"\x01")
            s.append(("Altitude", f"{-alt if below else alt:.0f} m"))
    if len(s) <= 2 and not info:
        s.append(("EXIF", "Aucune information EXIF dans ce fichier"))
    result["summary"] = s

    def named(d: dict, names: dict) -> list[tuple[str, str]]:
        out = []
        for tag in sorted(d):
            if tag in (0x8769, 0x8825, 0xA005):
                continue   # pointeurs vers les sous-répertoires
            name = names.get(tag, f"0x{tag:04X}")
            value = d[tag]
            if tag in _EXIF_SKIP_BINARY and isinstance(value, bytes):
                text = _user_comment(value) if tag == 0x9286 else f"(données binaires, {len(value)} octets)"
            else:
                text = _exif_text(value)
            out.append((name, text))
        return out

    groups = []
    if ifd0:
        groups.append(("Image (IFD0)", named(ifd0, ExifTags.TAGS)))
    if sub:
        groups.append(("Exif", named(sub, ExifTags.TAGS)))
    if gps:
        groups.append(("GPS", named(gps, ExifTags.GPSTAGS)))
    if info:
        groups.append(("Autres métadonnées du fichier", sorted(info.items())))
    result["groups"] = groups
    return result


def _read_exif_tool(tool: str, path: str) -> dict:
    """Repli sur exiftool (HEIC, RAW non lisibles par Pillow)."""
    result: dict = {"summary": [], "groups": [], "gps": None, "error": ""}
    try:
        out = subprocess.run([tool, "-json", "-G1", "-s", "--", path], capture_output=True,
                             timeout=30, check=False).stdout
        data = json.loads(out.decode("utf-8", "replace"))[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        result["error"] = f"Lecture des informations impossible : {exc}"
        return result
    groups: dict[str, list[tuple[str, str]]] = {}
    for k, v in data.items():
        if k == "SourceFile":
            continue
        g, _, name = k.partition(":")
        if not name:
            g, name = "Fichier", k
        groups.setdefault(g, []).append((name, _exif_text(v) if not isinstance(v, (int, float)) else str(v)))
    flat = {name: val for lst in groups.values() for name, val in lst}
    for key, label in (("Model", "Appareil"), ("LensModel", "Objectif"), ("DateTimeOriginal", "Date de prise de vue"),
                       ("ExposureTime", "Temps de pose"), ("FNumber", "Ouverture"), ("ISO", "Sensibilité"),
                       ("FocalLength", "Focale"), ("ImageSize", "Dimensions"), ("GPSPosition", "Coordonnées GPS")):
        if key in flat:
            result["summary"].append((label, flat[key]))
    result["groups"] = sorted(groups.items())
    return result


# ---------------------------------------------------------------------------
# Presse-papiers (image) : wl-copy, xclip, ou GTK en processus auxiliaire
# ---------------------------------------------------------------------------

CLIPBOARD_MAX_SIDE = 8000

# Processus auxiliaire : garde l'image dans le presse-papiers (X11 / XWayland) jusqu'à ce
# qu'une autre application en prenne possession. Tk ne sait copier que du texte.
_GTK_CLIP_HELPER = r"""
import sys
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
data = sys.stdin.buffer.read()
loader = GdkPixbuf.PixbufLoader.new_with_type("png"); loader.write(data); loader.close()
cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
cb.set_image(loader.get_pixbuf())
armed = [False]
def changed(*_):
    if armed[0]:
        Gtk.main_quit()
cb.connect("owner-change", changed)
GLib.timeout_add(800, lambda: armed.__setitem__(0, True) or False)
GLib.timeout_add_seconds(6 * 3600, lambda: Gtk.main_quit() or False)
print("ok", flush=True)
Gtk.main()
"""


def image_to_png(path: str) -> bytes:
    img = load_pil_image(path, CLIPBOARD_MAX_SIDE, CLIPBOARD_MAX_SIDE)
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=3)
    return buf.getvalue()


def clipboard_methods() -> list[str]:
    methods = []
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        methods.append("wl-copy")
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        methods.append("xclip")
    if os.environ.get("DISPLAY"):
        methods.append("gtk")
    return methods


def copy_png_to_clipboard(png: bytes) -> str:
    """Place une image PNG dans le presse-papiers. Renvoie la méthode utilisée, lève OSError sinon."""
    errors = []
    for method in clipboard_methods():
        try:
            if method == "wl-copy":
                cmd, env = ["wl-copy", "--type", "image/png"], None
            elif method == "xclip":
                cmd, env = ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"], None
            else:
                cmd = [sys.executable, "-c", _GTK_CLIP_HELPER]
                env = dict(os.environ, GDK_BACKEND="x11")
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                                    cwd=os.path.expanduser("~"))
            proc.stdin.write(png)
            proc.stdin.close()
            if method == "gtk":
                if proc.stdout.readline().strip() != b"ok":
                    raise OSError("GTK indisponible (paquets python3-gi et gir1.2-gtk-3.0)")
            else:
                try:
                    if proc.wait(timeout=5) != 0:
                        raise OSError(f"{method} a échoué")
                except subprocess.TimeoutExpired:
                    pass   # xclip reste actif pour servir le presse-papiers
            proc.stdout.close()
            return method
        except (OSError, ValueError) as exc:
            errors.append(f"{method} : {exc}")
    raise OSError("aucun moyen de copier une image dans le presse-papiers"
                  + (" (" + " ; ".join(errors) + ")" if errors else ""))


def resolve_photo_file(library, photo: Photo, original: bool = False) -> Optional[str]:
    """Fichier d'une photo sur le disque : version affichée (par défaut) ou original."""
    if photo.source == "folder":
        return photo.image_path if library.original_exists(photo) else None
    order = (photo.original_path, photo.image_path) if original else (photo.image_path, photo.original_path)
    for mp in order:
        if mp:
            real = library.existing(library.to_linux(mp))
            if real:
                return real
    return None


# ---------------------------------------------------------------------------
# Copie vers un dossier de destination (les fichiers sources ne sont que lus)
# ---------------------------------------------------------------------------

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "Sans titre") -> str:
    """Nom de fichier ou de dossier valide aussi sur les clés USB (FAT/exFAT)."""
    name = _BAD_NAME_CHARS.sub("_", safe_text(name)).strip().rstrip(".").strip()
    return name[:120] or fallback


def unique_path(directory: str, name: str, taken: Optional[set[str]] = None) -> str:
    """Chemin libre dans directory : « nom.jpg », sinon « nom (2).jpg »… (aucun écrasement)."""
    stem, ext = os.path.splitext(name)
    taken = taken if taken is not None else set()
    n = 1
    while True:
        candidate = name if n == 1 else f"{stem} ({n}){ext}"
        path = os.path.join(directory, candidate)
        if candidate.lower() not in taken and not os.path.lexists(path):
            taken.add(candidate.lower())
            return path
        n += 1


class CopyTask:
    """Copie des photos vers un dossier, dans un thread. Ne remplace jamais un fichier.

    items : liste (photo, sous-dossier relatif). resolve(photo) renvoie le fichier source
    (ou None s'il est introuvable) ; il est appelé dans le thread, car un disque externe
    peut être lent. Les fichiers sources ne sont ouverts qu'en lecture.
    """

    def __init__(self, items: list[tuple[Photo, str]], resolve: Callable[[Photo], Optional[str]],
                 dest: str, forbidden: list[str]):
        self.items = items
        self.resolve = resolve
        self.dest = dest
        self.forbidden = [r for r in forbidden if r]
        self.cancelled = threading.Event()
        self.phase = "prepare"
        self.total = len(items)
        self.done = 0
        self.copied = 0
        self.bytes_done = 0
        self.total_bytes = 0
        self.missing: list[str] = []
        self.failed: list[str] = []
        self.created_dirs: set[str] = set()
        self.finished = False
        self.fatal = ""

    def check_destination(self) -> None:
        """Contrôles rapides (dans le thread de l'interface) ; lève une erreur explicite."""
        if not os.path.isdir(self.dest):
            raise OSError(f"Le dossier de destination n'existe pas :\n{self.dest}")
        for root in self.forbidden:
            if is_inside(self.dest, root):
                raise PermissionError(
                    "La destination se trouve dans une bibliothèque ou un dossier consulté, qui sont "
                    f"en lecture seule :\n{root}\n\nChoisissez un autre dossier.")
        if not os.access(self.dest, os.W_OK | os.X_OK):
            raise PermissionError(f"Vous n'avez pas le droit d'écrire dans :\n{self.dest}")

    def _assert_target(self, path: str) -> None:
        for root in self.forbidden:
            if is_inside(path, root):
                raise PermissionError(f"Écriture refusée dans {root}")

    def run(self) -> None:
        try:
            jobs: list[tuple[str, str, str]] = []      # source, sous-dossier, libellé
            for photo, subdir in self.items:
                if self.cancelled.is_set():
                    return
                label = photo.filename or photo.label
                src = self.resolve(photo)
                if not src:
                    self.missing.append(label)
                    continue
                try:
                    self.total_bytes += os.path.getsize(src)
                except OSError:
                    self.missing.append(label)
                    continue
                jobs.append((src, subdir, label))
                self.done += 1
            free = shutil.disk_usage(self.dest).free
            if self.total_bytes > free:
                self.fatal = (f"Espace insuffisant sur la destination : {format_size(self.total_bytes)} "
                              f"nécessaires, {format_size(free)} disponibles.")
                return
            self.phase = "copy"
            self.total = len(jobs)
            self.done = 0
            taken: dict[str, set[str]] = {}
            for src, subdir, label in jobs:
                if self.cancelled.is_set():
                    break
                self._copy_one(src, subdir, label, taken)
                self.done += 1
        except Exception as exc:   # erreur inattendue : signalée sans planter l'application
            self.fatal = str(exc)
        finally:
            self.finished = True

    def _copy_one(self, src: str, subdir: str, label: str, taken: dict[str, set[str]]) -> None:
        directory = os.path.join(self.dest, subdir) if subdir else self.dest
        tmp = None
        try:
            self._assert_target(directory)
            if not os.path.isdir(directory):
                os.makedirs(directory)
                self.created_dirs.add(directory)
            names = taken.setdefault(directory, set())
            target = unique_path(directory, safe_filename(os.path.basename(src), "image"), names)
            tmp = target + ".iphotor-part"
            st = os.stat(src)
            with open(src, "rb") as fsrc, open(tmp, "xb") as fdst:
                while True:
                    if self.cancelled.is_set():
                        raise InterruptedError
                    chunk = fsrc.read(1 << 20)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    self.bytes_done += len(chunk)
            try:
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
            if os.path.lexists(target):            # apparu entre-temps : jamais d'écrasement
                target = unique_path(directory, os.path.basename(target), names)
            os.rename(tmp, target)
            tmp = None
            self.copied += 1
        except InterruptedError:
            pass
        except OSError as exc:
            self.failed.append(f"{label} : {exc.strerror or exc}")
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Interface graphique
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Design system : palettes, typographie, icônes vectorielles et styles ttk
# ---------------------------------------------------------------------------
# Toutes les couleurs de l'interface passent par COLORS (palette courante, modifiée
# en place lors d'un changement de thème). Les icônes et les éléments arrondis
# (boutons, champs, cases à cocher, barres de défilement) sont dessinés localement
# avec Pillow : aucune ressource externe, aucun accès réseau.

PALETTES = {
    "light": {
        "bg": "#F5F6F8", "panel": "#FFFFFF", "footer": "#F8F9FB", "header": "#F5F6F8",
        "card": "#FFFFFF", "card_border": "#E3E6EB", "card_hover_border": "#BCD0EE",
        "well": "#EEF0F3", "placeholder": "#E2E5EA",
        "text": "#202631", "muted": "#646F7F", "faint": "#A6AEBA",
        "accent": "#3978D5", "accent_fill": "#3978D5", "accent_hover": "#2F6AC4",
        "accent_pressed": "#2759A8", "accent_soft": "#E1EBFA", "accent_soft_text": "#1D54A3",
        "on_accent": "#FFFFFF", "secondary": "#5D6D85",
        "separator": "#E3E6EB", "field": "#F1F3F6", "field_border": "#DADEE5",
        "button": "#FFFFFF", "button_border": "#D5DAE1", "button_hover": "#F2F4F7",
        "button_pressed": "#E5E9EF", "ghost_hover": "#EDF0F4", "ghost_pressed": "#E1E6ED",
        "scroll_thumb": "#CAD0D8", "scroll_thumb_hover": "#A8B0BC", "row_hover": "#F3F5F8",
        "favorite": "#DE3F5C", "missing": "#CF3F3B", "video": "#D97F16", "star": "#F0AE1A",
        "success": "#2B8F5A", "viewer_bg": "#1D2026", "viewer_text": "#C7CFDA", "shadow": "#22304A",
    },
    "dark": {
        "bg": "#171A21", "panel": "#202530", "footer": "#1C2029", "header": "#171A21",
        "card": "#272E3A", "card_border": "#2E3644", "card_hover_border": "#4A6A9C",
        "well": "#1E232C", "placeholder": "#2C3340",
        "text": "#EDF1F7", "muted": "#A0AABA", "faint": "#6C7789",
        "accent": "#6AA6FF", "accent_fill": "#3370CF", "accent_hover": "#3D7BDA",
        "accent_pressed": "#2D63B6", "accent_soft": "#253957", "accent_soft_text": "#D2E3FF",
        "on_accent": "#FFFFFF", "secondary": "#93A3BC",
        "separator": "#2D3440", "field": "#181C24", "field_border": "#353D4B",
        "button": "#2A313D", "button_border": "#384150", "button_hover": "#323A48",
        "button_pressed": "#3A4352", "ghost_hover": "#2B323E", "ghost_pressed": "#343C4A",
        "scroll_thumb": "#3D4656", "scroll_thumb_hover": "#536074", "row_hover": "#262C38",
        "favorite": "#FF6B86", "missing": "#F06A64", "video": "#F2A145", "star": "#F7C445",
        "success": "#5BC48A", "viewer_bg": "#111318", "viewer_text": "#C7CFDA", "shadow": "#000000",
    },
}
THEMES = ("light", "dark")
COLORS: dict[str, str] = dict(PALETTES["light"])

# Polices système courantes sous Debian, par ordre de préférence.
FONT_FAMILIES = ("Inter", "Cantarell", "Noto Sans", "Ubuntu", "Open Sans", "DejaVu Sans", "Liberation Sans")

# Espacements (px) : une seule échelle pour toute l'interface.
SPACE_XS, SPACE_S, SPACE_M, SPACE_L, SPACE_XL = 4, 8, 12, 16, 24


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return (*_rgb(color), alpha)


def mix(c1: str, c2: str, t: float) -> str:
    """Mélange deux couleurs (t = 0 : c1, t = 1 : c2)."""
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_image(w: int, h: int, r: float, fill: Optional[str], border: Optional[str] = None,
                  bw: float = 1.0, bg: Optional[str] = None, fill_alpha: int = 255,
                  shadow: Optional[tuple[str, int, float]] = None, inset: int = 0):
    """Rectangle arrondi anticrénelé (image PIL). bg : couleur de fond opaque, sinon transparent.
    shadow = (couleur, opacité 0-255, flou en px) ; inset : marge réservée à l'ombre."""
    ss = 4
    W, H = w * ss, h * ss
    base = Image.new("RGBA", (W, H), _rgba(bg) if bg else (0, 0, 0, 0))
    box = [inset * ss, inset * ss, W - inset * ss - 1, H - inset * ss - 1]
    if shadow is not None:
        from PIL import ImageFilter
        col, alpha, blur = shadow
        sh = Image.new("L", (W, H), 0)
        ImageDraw.Draw(sh).rounded_rectangle([box[0], box[1] + ss, box[2], box[3] + ss], r * ss, fill=alpha)
        sh = sh.filter(ImageFilter.GaussianBlur(blur * ss))
        layer = Image.new("RGBA", (W, H), _rgba(col))
        layer.putalpha(sh)
        base = Image.alpha_composite(base, layer)
    shape = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(shape)
    if fill:
        d.rounded_rectangle(box, r * ss, fill=_rgba(fill, fill_alpha))
    if border:
        d.rounded_rectangle(box, r * ss, outline=_rgba(border), width=max(1, round(bw * ss)))
    base = Image.alpha_composite(base, shape)
    return base.resize((w, h), Image.LANCZOS)


class _Pen:
    """Dessin d'icônes « au trait » sur une grille 24 × 24, suréchantillonné (anticrénelage)."""

    SS = 4

    def __init__(self, size: int, color: str, stroke: float = 2.0):
        self.size = size
        self.k = size * self.SS / 24.0
        self.img = Image.new("RGBA", (size * self.SS, size * self.SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.c = _rgba(color)
        self.w = max(2, round(stroke * self.k))

    def p(self, x: float, y: float) -> tuple[float, float]:
        return x * self.k, y * self.k

    def line(self, *pts, closed: bool = False, erase: bool = False, width: Optional[float] = None) -> None:
        col = (0, 0, 0, 0) if erase else self.c
        w = self.w if width is None else max(2, round(width * self.k))
        xy = [self.p(*q) for q in pts]
        if closed:
            xy.append(xy[0])
        self.d.line(xy, fill=col, width=w, joint="curve")
        r = w / 2
        for x, y in xy:
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=col)

    def rrect(self, x0, y0, x1, y1, r, fill: bool = False, erase: bool = False) -> None:
        col = (0, 0, 0, 0) if erase else self.c
        box = [*self.p(x0, y0), *self.p(x1, y1)]
        if fill:
            self.d.rounded_rectangle(box, r * self.k, fill=col)
        else:
            self.d.rounded_rectangle(box, r * self.k, outline=col, width=self.w)

    def circle(self, cx, cy, r, fill: bool = False, erase: bool = False) -> None:
        col = (0, 0, 0, 0) if erase else self.c
        box = [*self.p(cx - r, cy - r), *self.p(cx + r, cy + r)]
        if fill:
            self.d.ellipse(box, fill=col)
        else:
            self.d.ellipse(box, outline=col, width=self.w)

    def poly(self, pts, erase: bool = False) -> None:
        self.d.polygon([self.p(*q) for q in pts], fill=(0, 0, 0, 0) if erase else self.c)

    def arc(self, cx, cy, r, start, end) -> None:
        box = [*self.p(cx - r, cy - r), *self.p(cx + r, cy + r)]
        self.d.arc(box, start, end, fill=self.c, width=self.w)

    def image(self):
        return self.img.resize((self.size, self.size), Image.LANCZOS)


def _heart_points(cx=12.0, cy=12.6, s=0.56, n=64):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * s, cy - y * s - 1.2))
    return pts


def _star_points(cx=12.0, cy=12.4, ro=9.5, ri=4.0):
    return [(cx + (ro if i % 2 == 0 else ri) * math.sin(math.pi * i / 5),
             cy - (ro if i % 2 == 0 else ri) * math.cos(math.pi * i / 5)) for i in range(10)]


def draw_icon(name: str, size: int, color: str):
    """Renvoie l'icône `name` (image PIL RGBA) dans la couleur demandée."""
    pen = _Pen(size, color, stroke=2.0 if size <= 18 else 1.8)
    P = pen
    if name == "photos":
        P.rrect(3, 3, 21, 21, 3.5)
        P.circle(9, 9, 2)
        P.line((21, 15.5), (16, 10.5), (5, 21))
    elif name == "album":
        P.rrect(7, 7, 21, 20, 2.5)
        P.line((4, 16.5), (4, 6), (5.5, 4.5), (16.5, 4.5))
        P.line((7.5, 18.5), (12, 14), (14.5, 16.5), (17, 14), (20.5, 17.5))
    elif name == "smart":
        P.rrect(3, 4, 21, 20, 3)
        P.poly(_star_points(12, 12.2, 5.6, 2.0))
    elif name == "event":
        P.rrect(3, 5, 21, 21, 3)
        P.line((3.5, 10), (20.5, 10))
        P.line((8, 3), (8, 6.5))
        P.line((16, 3), (16, 6.5))
        P.circle(8.5, 15, 1.4, fill=True)
    elif name == "folder":
        P.line((3, 6.5), (3, 18), (4.5, 19.5), (19.5, 19.5), (21, 18), (21, 10), (19.5, 8.5),
               (12.5, 8.5), (10.5, 5), (4.5, 5), closed=True)
    elif name == "folder_open":
        P.line((3, 18.5), (3, 6.5), (4.5, 5), (10, 5), (12, 8), (18, 8), (19.5, 9.5), (19.5, 11))
        P.line((3, 18.5), (6, 11.5), (22, 11.5), (19, 19.5), (4.5, 19.5), closed=True)
    elif name == "folder_arrow":
        P.line((3, 6.5), (3, 18), (4.5, 19.5), (19.5, 19.5), (21, 18), (21, 10), (19.5, 8.5),
               (12.5, 8.5), (10.5, 5), (4.5, 5), closed=True)
        P.line((8.5, 14), (15, 14))
        P.line((12.5, 11.5), (15, 14), (12.5, 16.5))
    elif name in ("heart", "heart_fill"):
        pts = _heart_points()
        if name == "heart_fill":
            P.poly(pts)
            P.line(*pts, closed=True)
        else:
            P.line(*pts, closed=True)
    elif name == "tag":
        P.line((3, 4.5), (4.5, 3), (11.5, 3), (21, 12.5), (21, 13.5), (13.5, 21), (12.5, 21), (3, 11.5),
               closed=True)
        P.circle(8, 8, 1.6, fill=True)
    elif name == "hash":
        P.line((9.5, 4), (7.5, 20))
        P.line((16.5, 4), (14.5, 20))
        P.line((4.5, 9), (19.5, 9))
        P.line((4, 15), (19, 15))
    elif name == "search":
        P.circle(10.5, 10.5, 6.5)
        P.line((15.5, 15.5), (20.5, 20.5))
    elif name == "close":
        P.line((6.5, 6.5), (17.5, 17.5))
        P.line((17.5, 6.5), (6.5, 17.5))
    elif name == "chev_left":
        P.line((15, 5), (8, 12), (15, 19))
    elif name == "chev_right":
        P.line((9, 5), (16, 12), (9, 19))
    elif name == "chev_down":
        P.line((6, 9), (12, 15), (18, 9))
    elif name == "info":
        P.circle(12, 12, 9)
        P.line((12, 11), (12, 16.5))
        P.circle(12, 7.6, 1.4, fill=True)
    elif name == "copy":
        P.rrect(8, 8, 21, 21, 2.5)
        P.line((4.5, 16), (3, 14.5), (3, 4.5), (4.5, 3), (14.5, 3), (16, 4.5))
    elif name == "file":
        P.line((6.5, 3), (14, 3), (19, 8), (19, 19.5), (17.5, 21), (6.5, 21), (5, 19.5), (5, 4.5), closed=True)
        P.line((14, 3.5), (14, 8), (18.5, 8))
        P.line((8.5, 13), (15.5, 13))
        P.line((8.5, 17), (13, 17))
    elif name == "external":
        P.line((14, 3), (21, 3), (21, 10))
        P.line((21, 3), (11.5, 12.5))
        P.line((18, 14), (18, 19.5), (16.5, 21), (4.5, 21), (3, 19.5), (3, 7.5), (4.5, 6), (10, 6))
    elif name == "video":
        P.circle(12, 12, 9)
        P.poly([(10, 8.2), (16.2, 12), (10, 15.8)])
    elif name == "play":
        P.poly([(7, 4.5), (19.5, 12), (7, 19.5)])
        P.line((7, 4.5), (19.5, 12), (7, 19.5), closed=True, width=1.5)
    elif name == "warning":
        P.line((12, 3.5), (21.5, 20), (2.5, 20), closed=True)
        P.line((12, 9.5), (12, 13.8))
        P.circle(12, 16.9, 1.3, fill=True)
    elif name == "image_off":
        P.rrect(3, 3, 21, 21, 3.5)
        P.circle(9, 9, 2)
        P.line((21, 15.5), (16, 10.5), (5, 21))
        P.line((2, 2), (22, 22), erase=True, width=5)
        P.line((3, 3), (21, 21))
    elif name == "library":
        P.rrect(3, 3, 10.5, 10.5, 2)
        P.rrect(13.5, 3, 21, 10.5, 2)
        P.rrect(3, 13.5, 10.5, 21, 2)
        P.rrect(13.5, 13.5, 21, 21, 2)
    elif name == "sun":
        P.circle(12, 12, 4.2)
        for i in range(8):
            a = math.pi * i / 4
            P.line((12 + 7.2 * math.cos(a), 12 + 7.2 * math.sin(a)), (12 + 9.6 * math.cos(a), 12 + 9.6 * math.sin(a)))
    elif name == "moon":
        P.circle(12, 12, 8.5, fill=True)
        P.circle(16.5, 7.5, 6.8, fill=True, erase=True)
        inner = _Pen(size, color)
        inner.circle(12, 12, 8.5 - 2.0 * 1.0, fill=True)
        inner.circle(16.5, 7.5, 6.8 + 2.0 * 1.0, fill=True, erase=True)
        cut = inner.img.getchannel("A")
        alpha = P.img.getchannel("A")
        from PIL import ImageChops
        P.img.putalpha(ImageChops.subtract(alpha, cut))
    elif name == "lock":
        P.rrect(5, 11, 19, 21, 2.5)
        P.arc(12, 7.5, 4, 180, 360)
        P.line((8, 7.5), (8, 11))
        P.line((16, 7.5), (16, 11))
    elif name == "star_fill":
        P.poly(_star_points())
    elif name == "check":
        P.line((5, 12.5), (10, 17.5), (19, 7))
    elif name == "dot":
        P.circle(12, 12, 3.5, fill=True)
    elif name == "refresh":
        P.arc(12, 12, 8, 210, 520 - 360 + 360)
        P.line((20, 4.5), (20, 9.5), (15, 9.5))
    else:  # icône inconnue : carré discret
        P.rrect(5, 5, 19, 19, 3)
    return pen.image()


def draw_logo(size: int, accent: str):
    """Logo d'iphotor : carré arrondi bleu, photo stylisée."""
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    top, bottom = _rgb(mix(accent, "#7FB2FF", 0.35)), _rgb(mix(accent, "#1E3F7A", 0.25))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / S
        gd.line([(0, y), (S, y)], fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], S * 0.24, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    k = S / 24
    d.ellipse([8.6 * k - 2.2 * k, 8.4 * k - 2.2 * k, 8.6 * k + 2.2 * k, 8.4 * k + 2.2 * k], fill=(255, 214, 102, 255))
    d.polygon([(3.5 * k, 19 * k), (9.5 * k, 12.5 * k), (13 * k, 16 * k), (15.8 * k, 13.2 * k), (20.5 * k, 19 * k)],
              fill=(255, 255, 255, 240))
    return img.resize((size, size), Image.LANCZOS)


class Icons:
    """Icônes de l'interface (PhotoImage), regénérées en place lors d'un changement de thème."""

    def __init__(self, master):
        self.master = master
        self._cache: dict[tuple[str, int, str], object] = {}

    def get(self, name: str, size: int = 16, role: str = "secondary"):
        key = (name, size, role)
        img = self._cache.get(key)
        if img is None:
            img = ImageTk.PhotoImage(self._render(*key), master=self.master)
            self._cache[key] = img
        return img

    @staticmethod
    def _render(name: str, size: int, role: str):
        if name == "logo":
            return draw_logo(size, COLORS["accent_fill"])
        color = "#FFFFFF" if role == "white" else COLORS.get(role, role if role.startswith("#") else COLORS["secondary"])
        return draw_icon(name, size, color)

    def refresh(self) -> None:
        for key, img in self._cache.items():
            img.paste(self._render(*key))


class Theme:
    """Styles ttk de l'application (clam personnalisé + éléments arrondis dessinés par Pillow)."""

    def __init__(self, root, fonts: dict):
        self.root = root
        self.fonts = fonts
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._images: dict[str, object] = {}
        self._created = False

    # -- images des éléments ---------------------------------------------------
    def _img(self, key: str, pil) -> object:
        img = self._images.get(key)
        if img is None:
            img = ImageTk.PhotoImage(pil, master=self.root)
            self._images[key] = img
        else:
            img.paste(pil)
        return img

    def _element_images(self) -> None:
        c = COLORS
        R = 7
        n = 2 * R + 6
        btn = lambda fill, border, bg="panel": rounded_image(n, n, R, c[fill], c[border] if border else None, 1, bg=c[bg])
        self._img("btn", btn("button", "button_border"))
        self._img("btn_hover", btn("button_hover", "button_border"))
        self._img("btn_pressed", btn("button_pressed", "button_border"))
        self._img("btn_disabled", btn("panel", "separator"))
        self._img("btn_focus", rounded_image(n, n, R, c["button"], c["accent"], 1.6, bg=c["panel"]))
        self._img("acc", btn("accent_fill", None))
        self._img("acc_hover", btn("accent_hover", None))
        self._img("acc_pressed", btn("accent_pressed", None))
        self._img("acc_disabled", rounded_image(n, n, R, mix(c["accent_fill"], c["panel"], 0.55), None, bg=c["panel"]))
        self._img("acc_focus", rounded_image(n, n, R, c["accent_fill"], mix(c["accent_fill"], c["text"], 0.45), 1.6,
                                             bg=c["panel"]))
        self._img("ghost", rounded_image(n, n, R, c["panel"], None, bg=c["panel"]))
        self._img("ghost_hover", btn("ghost_hover", None))
        self._img("ghost_pressed", btn("ghost_pressed", None))
        self._img("ghost_focus", rounded_image(n, n, R, c["panel"], c["accent"], 1.4, bg=c["panel"]))
        # Sélecteur de mode (contrôle segmenté)
        self._img("track", rounded_image(n, n, R + 1, c["field"], c["separator"], 1, bg=c["panel"]))
        self._img("seg", rounded_image(n, n, R - 1, c["field"], None, bg=c["field"]))
        self._img("seg_hover", rounded_image(n, n, R - 1, mix(c["field"], c["text"], 0.06), None, bg=c["field"]))
        self._img("seg_sel", rounded_image(n, n, R - 1, c["panel"], c["button_border"], 1, bg=c["field"]))
        # Champ de recherche
        self._img("field", rounded_image(n, n, R + 1, c["field"], c["field_border"], 1, bg=c["panel"]))
        self._img("field_focus", rounded_image(n, n, R + 1, c["panel"], c["accent"], 1.6, bg=c["panel"]))
        # Cases à cocher et boutons radio (16 px)
        s = 18
        box = lambda fill, border: rounded_image(s, s, 4.5, c[fill], c[border] if border else None, 1.2,
                                                 bg=c["panel"], inset=1)
        chk = box("accent_fill", None)
        chk.alpha_composite(draw_icon("check", s, "#FFFFFF"))
        def margin(im):   # espace entre la case et son libellé
            out = Image.new("RGBA", (s + 7, s), _rgba(c["panel"]))
            out.alpha_composite(im.convert("RGBA"))
            return out
        self._img("chk_off", margin(box("button", "button_border")))
        self._img("chk_hover", margin(box("button", "accent")))
        self._img("chk_on", margin(chk))
        self._img("chk_dis", margin(box("panel", "separator")))
        rad = lambda fill, border, bw=1.2: rounded_image(s, s, 8, c[fill], c[border] if border else None, bw,
                                                         bg=c["panel"], inset=1)
        on = rad("accent_fill", None)
        dot = rounded_image(s, s, 3, "#FFFFFF", None, inset=6)
        on.alpha_composite(dot)
        self._img("rad_off", margin(rad("button", "button_border")))
        self._img("rad_hover", margin(rad("button", "accent")))
        self._img("rad_on", margin(on))
        self._img("rad_dis", margin(rad("panel", "separator")))
        # Indicateurs de l'arborescence (transparents)
        self._img("tree_closed", draw_icon("chev_right", 14, c["faint"]))
        self._img("tree_open", draw_icon("chev_down", 14, c["muted"]))
        self._img("tree_leaf", Image.new("RGBA", (14, 14), (0, 0, 0, 0)))
        # Barres de défilement fines
        for surf in ("panel", "bg"):
            for st, col in (("", "scroll_thumb"), ("_hover", "scroll_thumb_hover")):
                self._img(f"thumb_{surf}{st}", rounded_image(12, 24, 3.5, c[col], None, bg=c[surf] if surf != "bg"
                                                             else c["bg"], inset=3))
            self._img(f"trough_{surf}", Image.new("RGB", (12, 12), _rgb(c[surf])))
        # Barre de progression
        for surf in ("panel", "bg"):
            self._img(f"ptrough_{surf}", rounded_image(16, 8, 4, mix(c[surf], c["text"], 0.1), None, bg=c[surf]))
            self._img(f"pbar_{surf}", rounded_image(16, 8, 4, c["accent_fill"], None, bg=c[surf]))

    def _create_elements(self) -> None:
        st = self.style
        I = self._images
        ce = st.element_create
        ce("Iph.Button.bg", "image", I["btn"], ("disabled", I["btn_disabled"]), ("pressed", I["btn_pressed"]),
           ("active", I["btn_hover"]), ("focus", I["btn_focus"]), border=8, padding=3, sticky="nsew")
        ce("Iph.Accent.bg", "image", I["acc"], ("disabled", I["acc_disabled"]), ("pressed", I["acc_pressed"]),
           ("active", I["acc_hover"]), ("focus", I["acc_focus"]), border=8, padding=3, sticky="nsew")
        ce("Iph.Ghost.bg", "image", I["ghost"], ("pressed", I["ghost_pressed"]), ("active", I["ghost_hover"]),
           ("focus", I["ghost_focus"]), border=8, padding=3, sticky="nsew")
        ce("Iph.Track.bg", "image", I["track"], border=9, padding=0, sticky="nsew")
        ce("Iph.Seg.bg", "image", I["seg"], ("selected", I["seg_sel"]), ("active", I["seg_hover"]),
           border=8, padding=3, sticky="nsew")
        ce("Iph.Field.bg", "image", I["field"], border=9, padding=0, sticky="nsew")
        ce("Iph.FieldFocus.bg", "image", I["field_focus"], border=9, padding=0, sticky="nsew")
        ce("Iph.Check.indicator", "image", I["chk_off"], ("disabled", I["chk_dis"]), ("selected", I["chk_on"]),
           ("active", I["chk_hover"]), sticky="")
        ce("Iph.Radio.indicator", "image", I["rad_off"], ("disabled", I["rad_dis"]), ("selected", I["rad_on"]),
           ("active", I["rad_hover"]), sticky="")
        ce("Iph.Treeitem.indicator", "image", I["tree_closed"], ("user1", "!user2", I["tree_open"]),
           ("user2", I["tree_leaf"]), sticky="", padding=(0, 0, 4, 0))
        for surf in ("panel", "bg"):
            ce(f"Iph.{surf}.thumb", "image", I[f"thumb_{surf}"], ("active", I[f"thumb_{surf}_hover"]),
               ("pressed", I[f"thumb_{surf}_hover"]), border=(0, 8), sticky="nsew")
            ce(f"Iph.{surf}.trough", "image", I[f"trough_{surf}"], border=0, sticky="nsew")
            ce(f"Iph.{surf}.ptrough", "image", I[f"ptrough_{surf}"], border=4, sticky="nsew")
            ce(f"Iph.{surf}.pbar", "image", I[f"pbar_{surf}"], border=4, sticky="nsew")

        def btn_layout(element):
            return [(element, {"sticky": "nsew", "children": [
                ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})]

        st.layout("TButton", btn_layout("Iph.Button.bg"))
        st.layout("Accent.TButton", btn_layout("Iph.Accent.bg"))
        st.layout("Ghost.TButton", btn_layout("Iph.Ghost.bg"))
        st.layout("Icon.TButton", btn_layout("Iph.Ghost.bg"))
        st.layout("AccentIcon.TButton", btn_layout("Iph.Accent.bg"))
        st.layout("Segment.TFrame", [("Iph.Track.bg", {"sticky": "nsew"})])
        st.layout("Search.TFrame", [("Iph.Field.bg", {"sticky": "nsew"})])
        st.layout("SearchFocus.TFrame", [("Iph.FieldFocus.bg", {"sticky": "nsew"})])
        st.layout("Segment.Toolbutton", [("Iph.Seg.bg", {"sticky": "nsew", "children": [
            ("Toolbutton.padding", {"sticky": "nsew", "children": [("Toolbutton.label", {"sticky": "nsew"})]})]})])
        st.layout("Search.TEntry", [("Entry.field", {"sticky": "nsew", "border": "1", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        st.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Iph.Check.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        st.layout("TRadiobutton", [("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Iph.Radio.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Radiobutton.label", {"sticky": "nswe"})]})]})])
        st.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Iph.Treeitem.indicator", {"side": "left", "sticky": ""}),
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"sticky": "nswe"})]})])
        for surf, name in (("panel", "Vertical.TScrollbar"), ("bg", "Grid.Vertical.TScrollbar")):
            st.layout(name, [(f"Iph.{surf}.trough", {"sticky": "ns", "children": [
                (f"Iph.{surf}.thumb", {"expand": "1", "sticky": "nswe"})]})])
        for surf, name in (("panel", "Horizontal.TProgressbar"), ("bg", "Grid.Horizontal.TProgressbar")):
            st.layout(name, [(f"Iph.{surf}.ptrough", {"sticky": "nswe", "children": [
                (f"Iph.{surf}.pbar", {"side": "left", "sticky": "ns"})]})])

    # -- application ---------------------------------------------------------------
    def apply(self) -> None:
        """(Ré)applique la palette COLORS courante à tous les styles."""
        self._element_images()
        if not self._created:
            self._create_elements()
            self._created = True
        c, f = COLORS, self.fonts
        st = self.style
        st.configure(".", background=c["panel"], foreground=c["text"], fieldbackground=c["field"],
                     bordercolor=c["button_border"], lightcolor=c["panel"], darkcolor=c["panel"],
                     troughcolor=c["field"], selectbackground=c["accent_soft"], selectforeground=c["text"],
                     insertcolor=c["text"], focuscolor=c["accent"], font=f["base"])
        st.map(".", foreground=[("disabled", c["faint"])])
        for name, bg in (("TFrame", "panel"), ("Panel.TFrame", "panel"), ("Bg.TFrame", "bg"),
                         ("Footer.TFrame", "footer"), ("Header.TFrame", "header"), ("Field.TFrame", "field")):
            st.configure(name, background=c[bg])
        labels = {
            "TLabel": ("panel", "text", "base"), "Panel.TLabel": ("panel", "text", "base"),
            "Muted.TLabel": ("panel", "muted", "base"), "Small.TLabel": ("panel", "muted", "small"),
            "Favorite.TLabel": ("panel", "favorite", "base"), "Title.TLabel": ("panel", "text", "title"),
            "Brand.TLabel": ("panel", "text", "brand"), "Section.TLabel": ("panel", "muted", "caption"),
            "Heading.TLabel": ("header", "text", "h1"), "Subheading.TLabel": ("header", "muted", "base"),
            "HeaderMuted.TLabel": ("header", "muted", "small"), "Header.TLabel": ("header", "text", "base"),
            "Status.TLabel": ("footer", "muted", "small"), "StatusIcon.TLabel": ("footer", "muted", "small"),
            "Footer.TLabel": ("footer", "muted", "small"), "Field.TLabel": ("field", "muted", "base"),
            "Error.TLabel": ("panel", "missing", "base"), "Strong.TLabel": ("panel", "text", "bold"),
            "Chip.TLabel": ("accent_soft", "accent_soft_text", "small"),
            "FavChip.TLabel": ("panel", "favorite", "bold"),
        }
        for name, (bg, fg, font) in labels.items():
            st.configure(name, background=c[bg], foreground=c[fg], font=f[font])
        st.configure("Chip.TLabel", padding=(8, 2))
        for name in ("TButton", "Accent.TButton", "Ghost.TButton", "Icon.TButton"):
            st.configure(name, font=f["base"], anchor="center", relief="flat", borderwidth=0)
        st.configure("TButton", foreground=c["text"], padding=(12, 3))
        st.configure("Accent.TButton", foreground=c["on_accent"], padding=(14, 3), font=f["bold"])
        st.map("Accent.TButton", foreground=[("disabled", mix(c["on_accent"], c["panel"], 0.25))])
        st.configure("AccentIcon.TButton", padding=(8, 3), width=0)
        st.configure("TButton", width=-9)
        st.configure("Accent.TButton", width=-9)
        st.configure("Ghost.TButton", foreground=c["text"], padding=(8, 4), width=0)
        st.configure("Icon.TButton", foreground=c["text"], padding=(5, 4), width=0)
        st.map("TButton", foreground=[("disabled", c["faint"])])
        st.map("Ghost.TButton", foreground=[("disabled", c["faint"])])
        st.map("Icon.TButton", foreground=[("disabled", c["faint"])])
        st.configure("Segment.TFrame", padding=3)
        st.configure("Segment.Toolbutton", foreground=c["muted"], padding=(10, 2), font=f["base"], anchor="center",
                     width=0)
        st.map("Segment.Toolbutton", foreground=[("selected", c["text"]), ("active", c["text"])],
               font=[("selected", f["bold"])])
        st.configure("Search.TFrame", padding=(8, 1, 3, 1))
        st.configure("SearchFocus.TFrame", padding=(8, 1, 3, 1))
        st.configure("Search.TEntry", fieldbackground=c["field"], foreground=c["text"], padding=(4, 4),
                     insertcolor=c["text"], insertwidth=1, bordercolor=c["field"], lightcolor=c["field"],
                     darkcolor=c["field"])
        st.map("Search.TEntry", fieldbackground=[("focus", c["panel"])], bordercolor=[("focus", c["panel"])],
               lightcolor=[("focus", c["panel"])], darkcolor=[("focus", c["panel"])])
        st.configure("SearchIcon.TLabel", background=c["field"], foreground=c["muted"])
        st.configure("SearchIconFocus.TLabel", background=c["panel"], foreground=c["muted"])
        st.configure("SearchClear.TButton", padding=(4, 3))
        st.layout("SearchClear.TButton", [("Button.padding", {"sticky": "nsew", "children": [
            ("Button.label", {"sticky": "nsew"})]})])
        st.configure("SearchClear.TButton", background=c["field"])
        st.map("SearchClear.TButton", background=[("active", c["ghost_hover"])])
        st.configure("SearchClearFocus.TButton", padding=(4, 3), background=c["panel"])
        st.layout("SearchClearFocus.TButton", st.layout("SearchClear.TButton"))
        st.map("SearchClearFocus.TButton", background=[("active", c["ghost_hover"])])
        st.configure("TEntry", fieldbackground=c["panel"], foreground=c["text"], padding=(8, 5),
                     bordercolor=c["field_border"], lightcolor=c["field_border"], darkcolor=c["field_border"])
        st.map("TEntry", bordercolor=[("focus", c["accent"])], lightcolor=[("focus", c["accent"])],
               darkcolor=[("focus", c["accent"])])
        st.configure("TCombobox", fieldbackground=c["button"], background=c["button"], foreground=c["text"],
                     bordercolor=c["button_border"], lightcolor=c["button"], darkcolor=c["button"],
                     arrowcolor=c["muted"], arrowsize=12, padding=(8, 4))
        st.map("TCombobox", fieldbackground=[("readonly", c["button"])], foreground=[("readonly", c["text"])],
               background=[("active", c["button_hover"]), ("readonly", c["button"])],
               bordercolor=[("focus", c["accent"])], selectbackground=[("readonly", c["button"])],
               selectforeground=[("readonly", c["text"])], arrowcolor=[("active", c["text"])])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent_soft"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["accent_soft_text"])
        self.root.option_add("*TCombobox*Listbox.font", f["base"])
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)
        for name in ("TCheckbutton", "TRadiobutton"):
            st.configure(name, background=c["panel"], foreground=c["text"], padding=(0, 3), font=f["base"])
            st.map(name, background=[("active", c["panel"])], foreground=[("disabled", c["faint"])])
        st.configure("Treeview", background=c["panel"], fieldbackground=c["panel"], foreground=c["text"],
                     rowheight=28, borderwidth=0, relief="flat", font=f["base"], indent=16)
        st.map("Treeview", background=[("selected", c["accent_soft"])],
               foreground=[("selected", c["accent_soft_text"])])
        st.configure("Treeview.Heading", background=c["footer"], foreground=c["muted"], relief="flat",
                     borderwidth=0, font=f["small_bold"], padding=(8, 5))
        st.map("Treeview.Heading", background=[("active", c["button_hover"])])
        st.configure("Sidebar.Treeview", background=c["panel"], fieldbackground=c["panel"], rowheight=30,
                     indent=14, padding=(6, 4))
        st.configure("Item.Treeview", rowheight=26)
        st.layout("Sidebar.Treeview", [("Treeview.field", {"sticky": "nswe", "border": "0", "children": [
            ("Treeview.padding", {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]})]})])
        st.configure("TSeparator", background=c["separator"], lightcolor=c["separator"],
                     darkcolor=c["separator"], bordercolor=c["separator"])
        st.configure("Vertical.TScrollbar", width=12, arrowsize=0)
        st.configure("Grid.Vertical.TScrollbar", width=12, arrowsize=0)
        st.configure("Horizontal.TProgressbar", thickness=8)
        st.configure("Grid.Horizontal.TProgressbar", thickness=8)
        st.configure("TPanedwindow", background=c["separator"])
        # Menus Tk (barre de menus, menus contextuels)
        for opt, val in (("background", c["panel"]), ("foreground", c["text"]),
                         ("activeBackground", c["accent_soft"]), ("activeForeground", c["accent_soft_text"]),
                         ("disabledForeground", c["faint"]), ("selectColor", c["accent"]),
                         ("relief", "flat"), ("activeBorderWidth", 0), ("borderWidth", 1), ("font", f["base"])):
            self.root.option_add(f"*Menu.{opt}", val)
        # Boîtes de dialogue standard de Tk (messages, choix de dossier)
        self.root.option_add("*Dialog.msg.font", f["base"])
        self.root.option_add("*Dialog.dtl.font", f["base"])
        self.root.option_add("*Background", c["panel"])
        self.root.option_add("*Foreground", c["text"])
        self.root.option_add("*selectBackground", c["accent_soft"])
        self.root.option_add("*selectForeground", c["accent_soft_text"])
        self.root.option_add("*highlightColor", c["accent"])
        self.root.option_add("*highlightBackground", c["panel"])
        self.root.option_add("*Listbox.background", c["panel"])
        self.root.option_add("*Entry.background", c["panel"])
        self.root.option_add("*Text.background", c["panel"])


def pick_font_family(root) -> str:
    try:
        available = {f.lower(): f for f in tkfont.families(root)}
    except tk.TclError:
        available = {}
    for fam in FONT_FAMILIES:
        if fam.lower() in available:
            return available[fam.lower()]
    return tkfont.nametofont("TkDefaultFont").cget("family")


def setup_fonts(root) -> dict:
    """Polices de l'application : une seule famille système, hiérarchie par taille et graisse."""
    fam = pick_font_family(root)
    for name, size, weight in (("TkDefaultFont", 10, "normal"), ("TkTextFont", 10, "normal"),
                               ("TkMenuFont", 10, "normal"), ("TkHeadingFont", 10, "bold"),
                               ("TkCaptionFont", 11, "bold"), ("TkTooltipFont", 9, "normal"),
                               ("TkSmallCaptionFont", 9, "normal"), ("TkIconFont", 10, "normal")):
        try:
            tkfont.nametofont(name).configure(family=fam, size=size, weight=weight)
        except tk.TclError:
            pass
    base = tkfont.nametofont("TkDefaultFont")
    mk = lambda size, weight="normal": tkfont.Font(root=root, family=fam, size=size, weight=weight)
    return {
        "family": fam, "base": base, "small": mk(9), "small_bold": mk(9, "bold"), "bold": mk(10, "bold"),
        "caption": mk(8, "bold"), "title": mk(13, "bold"), "brand": mk(13, "bold"), "h1": mk(16, "bold"),
        "big": mk(12), "badge": mk(8, "bold"),
    }


class Tooltip:
    """Info-bulle discrète (utile pour les boutons réduits à leur icône)."""

    def __init__(self, widget, text: str, delay: int = 600):
        self.widget, self.text, self.delay = widget, text, delay
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._hide()
        self._job = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if not self.text or not self.widget.winfo_viewable():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(bg=COLORS["text"])
        tk.Label(tip, text=self.text, bg=COLORS["text"], fg=COLORS["panel"], padx=8, pady=3,
                 font=tkfont.nametofont("TkTooltipFont")).pack()
        tip.update_idletasks()
        tip.geometry(f"+{x - tip.winfo_width() // 2}+{y}")
        self._tip = tip

    def _hide(self, _e=None) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class PhotoViewer:
    """Fenêtre de visualisation d'une photo, avec navigation."""

    # (action, libellé, icône, info-bulle avec raccourci)
    ACTIONS = (
        ("fav", "Favori", "heart", "Ajouter aux favoris / retirer (F)"),
        ("tags", "Tags", "tag", "Tags de la photo (T)"),
        ("exif", "Infos", "info", "Informations EXIF (I)"),
        ("copy", "Copier", "copy", "Copier l'image dans le presse-papiers (Ctrl+C)"),
        ("path", "Chemin", "file", "Copier le chemin du fichier (Ctrl+Maj+C)"),
        ("to", "Copier vers…", "folder_arrow", "Copier vers un dossier (Ctrl+S)"),
        ("open", "Ouvrir", "external", "Ouvrir avec l'application par défaut"),
    )

    def __init__(self, app: "MainWindow"):
        self.app = app
        icons = app.icons
        self.win = tk.Toplevel(app.root)
        self.win.title("Visualisation — iphotor")
        self.win.configure(bg=COLORS["panel"])
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        self.max_w, self.max_h = min(sw, 3000), min(sh, 2000)
        self.win.geometry(f"{int(sw * 0.75)}x{int(sh * 0.8)}")
        self.win.minsize(560, 420)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.ids: list[int] = []
        self.index = 0
        self.photo: Optional[Photo] = None
        self.pil = None
        self.tkimg = None
        self.request_no = 0
        self.results: "queue.Queue[tuple]" = queue.Queue()
        self._resize_job = None
        self._details = ""
        self._compact: Optional[bool] = None

        # Barre d'outils : navigation à gauche, actions sur la photo à droite
        bar = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_M, SPACE_S))
        bar.pack(side="top", fill="x")
        self.btn_prev = ttk.Button(bar, image=(icons.get("chev_left", 18, "text"), "disabled",
                                               icons.get("chev_left", 18, "faint")),
                                   style="Icon.TButton", command=self.prev)
        self.btn_prev.pack(side="left")
        Tooltip(self.btn_prev, "Photo précédente (←)")
        self.btn_next = ttk.Button(bar, image=(icons.get("chev_right", 18, "text"), "disabled",
                                               icons.get("chev_right", 18, "faint")),
                                   style="Icon.TButton", command=self.next)
        self.btn_next.pack(side="left", padx=(2, 0))
        Tooltip(self.btn_next, "Photo suivante (→)")
        self.pos_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.pos_label.pack(side="left", padx=(SPACE_M, 0))
        self.action_buttons: dict[str, ttk.Button] = {}
        for action, label, icon, tip in reversed(self.ACTIONS):
            b = ttk.Button(bar, text=label, image=icons.get(icon, 16, "secondary"), compound="left",
                           style="Ghost.TButton",
                           command=self.open_external if action == "open" else (lambda a=action: self._act(a)))
            b.pack(side="right", padx=(2, 0))
            Tooltip(b, tip)
            self.action_buttons[action] = b
        self.btn_fav = self.action_buttons["fav"]
        self.btn_open = self.action_buttons["open"]
        ttk.Separator(self.win, orient="horizontal").pack(side="top", fill="x")

        # Informations (sous la photo)
        info = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_M, SPACE_XL, SPACE_L))
        info.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        head = ttk.Frame(info, style="Panel.TFrame")
        head.pack(fill="x")
        self.info_fav = ttk.Label(head, text=" Favori", image=icons.get("heart_fill", 14, "favorite"),
                                  compound="left", style="FavChip.TLabel")
        self.info_annot = ttk.Label(head, text="", image=icons.get("tag", 14, "accent_soft_text"),
                                    compound="left", style="Chip.TLabel")
        self.info_title = ttk.Label(head, text="", style="Title.TLabel")
        self.info_title.pack(side="left", fill="x", expand=True, anchor="w")
        self.info_details = ttk.Label(info, text="", style="Muted.TLabel", justify="left")
        self.info_details.pack(anchor="w", pady=(SPACE_XS, 0))
        self.info_comment = ttk.Label(info, text="", style="Small.TLabel", justify="left")
        self.info_comment.pack(anchor="w", pady=(2, 0))

        # Image : fond neutre sombre, la photo reste l'élément central
        self.canvas = tk.Canvas(self.win, bg=COLORS["viewer_bg"], highlightthickness=0)
        self.canvas._viewer = True
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.win.bind("<Configure>", self._on_win_resize, add="+")

        for key in ("<Left>", "<Prior>", "<BackSpace>"):
            self.win.bind(key, lambda e: self.prev())
        for key in ("<Right>", "<Next>", "<space>"):
            self.win.bind(key, lambda e: self.next())
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Home>", lambda e: self.show_index(0))
        self.win.bind("<End>", lambda e: self.show_index(len(self.ids) - 1))
        for seq, action in (("<Control-c>", "copy"), ("<Control-C>", "path"), ("<Control-s>", "to"),
                            ("<KeyPress-f>", "fav"), ("<KeyPress-t>", "tags"), ("<KeyPress-i>", "exif")):
            self.win.bind(seq, lambda e, a=action: self._act(a))
        self.canvas.bind("<Button-3>", self._on_menu)
        self.canvas.tag_bind("navprev", "<Button-1>", lambda e: self.prev())
        self.canvas.tag_bind("navnext", "<Button-1>", lambda e: self.next())
        for t in ("navprev", "navnext"):
            self.canvas.tag_bind(t, "<Enter>", lambda e: self.canvas.configure(cursor="hand2"))
            self.canvas.tag_bind(t, "<Leave>", lambda e: self.canvas.configure(cursor=""))
        self._poll()

    def _on_win_resize(self, event) -> None:
        if event.widget is not self.win:
            return
        compact = event.width < 1080
        if compact != self._compact:
            self._compact = compact
            for b in self.action_buttons.values():
                b.configure(compound="image" if compact else "left",
                            style="Icon.TButton" if compact else "Ghost.TButton")

    def apply_theme(self) -> None:
        self.win.configure(bg=COLORS["panel"])
        self.canvas.configure(bg=COLORS["viewer_bg"])
        if self.pil is not None:
            self._render()

    # -- actions sur la photo affichée ---------------------------------------------
    def _act(self, action: str) -> None:
        p = self.photo
        if p is None:
            return
        app = self.app
        {"copy": lambda: app.copy_image(p, self.win), "path": lambda: app.copy_path(p),
         "to": lambda: app.copy_photo_to(p, self.win), "fav": lambda: app.toggle_favorite(p, self.win),
         "tags": lambda: app.edit_tags(p, self.win), "exif": lambda: app.show_exif(p, self.win)}[action]()

    def _on_menu(self, event) -> None:
        if self.photo is not None:
            self.app.photo_menu(self.photo, self.win, event.x_root, event.y_root)

    def refresh_annotations(self) -> None:
        p = self.photo
        self.info_fav.pack_forget()
        self.info_annot.pack_forget()
        if p is None:
            self.info_annot.config(text="")
            return
        fav = self.app.is_favorite(p)
        self.btn_fav.config(image=self.app.icons.get("heart_fill" if fav else "heart", 16,
                                                     "favorite" if fav else "secondary"),
                            text="Favori ✓" if fav else "Favori")
        tags = self.app.tags_of(p)
        self.info_annot.config(text=" " + "  ·  ".join(tags) if tags else "")
        if tags:
            self.info_annot.pack(side="right", padx=(SPACE_S, 0))
        if fav:
            self.info_fav.pack(side="right", padx=(SPACE_S, 0))

    # -- navigation --------------------------------------------------------
    def show(self, ids: list[int], index: int) -> None:
        self.ids = ids
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.show_index(index)

    def show_index(self, index: int) -> None:
        if not self.ids:
            return
        index = max(0, min(index, len(self.ids) - 1))
        self.index = index
        self.photo = self.app.photos.get(self.ids[index])
        self.btn_prev.state(["!disabled"] if index > 0 else ["disabled"])
        self.btn_next.state(["!disabled"] if index < len(self.ids) - 1 else ["disabled"])
        noun = "Image" if self.photo is not None and self.photo.source == "folder" else "Photo"
        self.pos_label.config(text=f"{noun} {format_count(index + 1)} sur {format_count(len(self.ids))}")
        self._update_info()
        self._load_current()
        self.app.sync_page_to_index(index)

    def prev(self) -> None:
        if self.index > 0:
            self.show_index(self.index - 1)

    def next(self) -> None:
        if self.index < len(self.ids) - 1:
            self.show_index(self.index + 1)

    def close(self) -> None:
        self.pil = None
        self.tkimg = None
        self.canvas.delete("all")
        self.win.withdraw()
        self.app.viewer_closed()

    # -- affichage -----------------------------------------------------------
    def _update_info(self) -> None:
        p = self.photo
        if p is None:
            self.info_title.config(text="Photo introuvable dans l'index")
            self.info_details.config(text="")
            self.info_comment.config(text="")
            self.info_annot.config(text="")
            return
        self.win.title(f"{p.filename or p.label} — iphotor")
        self.info_title.config(text=p.caption or p.filename)
        self.refresh_annotations()
        terms = self.app.apple_terms_text(p)
        if p.source == "folder":
            details = [f"Dossier : {p.folder or '.'}"]
            if p.date is not None:
                details.append(f"Modifiée le : {format_date(p.date)}")
            details.append(f"Taille : {format_size(p.size)}")
            self._details = "   ·   ".join(details)
            self.info_details.config(text=self._details)
            self.info_comment.config(text=f"Chemin : {safe_text(p.image_path)}")
            return
        details = [f"Fichier : {p.filename or '—'}"]
        if p.date is not None:
            details.append(f"Date : {format_date(p.date)}")
        if p.rating:
            details.append("Note : " + "★" * max(0, min(p.rating, 5)) + "☆" * (5 - max(0, min(p.rating, 5))))
        if p.event_name:
            details.append(f"Événement : {p.event_name}")
        if p.is_video:
            details.append("Type : vidéo")
        self._details = "   ·   ".join(details)
        self.info_details.config(text=self._details)
        comment = p.comment if p.comment and p.comment != p.caption else ""
        text = f"Commentaire : {comment}" if comment else ""
        if terms:
            text = f"{text}   ·   {terms}" if text else terms
        self.info_comment.config(text=text)

    def _message(self, text: str, color: Optional[str] = None, error: bool = False) -> None:
        self.canvas.delete("all")
        w, h = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        icon = self.app.icons.get("image_off" if error else "photos", 48, "#6E7887")
        self.canvas.create_image(w // 2, h // 2 - 44, image=icon)
        self.canvas.create_text(w // 2, h // 2 + 14, text=text, fill=color or COLORS["viewer_text"],
                                font=self.app.font_big, justify="center", width=w - 80, anchor="n")
        self._draw_nav(w, h)

    def _draw_nav(self, cw: int, ch: int) -> None:
        """Boutons « précédente / suivante » en surimpression, de part et d'autre de la photo."""
        if cw < 260:
            return
        for tag, x, icon, ok in (("navprev", 36, "chev_left", self.index > 0),
                                 ("navnext", cw - 36, "chev_right", self.index < len(self.ids) - 1)):
            if ok:
                self.canvas.create_image(x, ch // 2, image=self.app.overlay_button(icon), tags=(tag,))

    def _load_current(self) -> None:
        p = self.photo
        self.pil = None
        self.tkimg = None
        self.request_no += 1
        if p is None:
            self._message("Photo introuvable dans l'index.")
            return
        self._message("Chargement…")
        no = self.request_no
        threading.Thread(target=self._worker, args=(no, p), daemon=True).start()

    def _worker(self, no: int, p: Photo) -> None:
        lib = self.app.library
        try:
            path, origin = lib.find_display_file(p, for_thumbnail=False)
            if not path:
                if not lib.is_available():
                    self.results.put((no, None, "Bibliothèque inaccessible : le disque externe n'est pas connecté.", "", None))
                elif file_ext(p.image_path) in RAW_EXTS and not p.original_path:
                    self.results.put((no, None, "Format RAW sans aperçu JPEG exploitable : affichage impossible.", "", None))
                else:
                    self.results.put((no, None, "Fichier introuvable sur le disque.\n" + safe_text(lib.to_linux(p.image_path) or ""), "", None))
                return
            img = load_pil_image(path, self.max_w, self.max_h)
            note = ""
            if p.is_video:
                note = "Vidéo — aperçu uniquement (lecture non prise en charge dans cette version)"
            elif origin == "thumb":
                note = "Original indisponible : affichage de la miniature"
            elif file_ext(path) in RAW_TIFF_EXTS:
                note = "Aperçu JPEG intégré au fichier RAW"
            elif "resources/media/version/" in path:
                note = "Version retouchée (rendu enregistré par Photos)"
            elif "resources/media/master/" in path:
                note = "Rendu JPEG du fichier RAW (enregistré par Photos)"
            elif origin == "image" and "/Previews/" in path:
                note = "Version développée (Previews)"
            self.results.put((no, img, "", note, image_size(path)))
        except Exception as exc:
            detail = "format d'image non reconnu" if "cannot identify image file" in str(exc) else str(exc)
            self.results.put((no, None, f"Impossible d'ouvrir cette image (fichier illisible ou corrompu).\n{detail}", "", None))

    def _poll(self) -> None:
        try:
            while True:
                no, img, err, note, dims = self.results.get_nowait()
                if no != self.request_no:
                    continue
                if dims:
                    self.info_details.config(
                        text=f"{self._details}   ·   {dims[0]} × {dims[1]} px")
                if img is None:
                    self._message(err, COLORS["missing"], error=True)
                else:
                    self.pil = img
                    self.note = note
                    self._render()
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(40, self._poll)

    def _on_resize(self, _event=None) -> None:
        if self._resize_job:
            self.win.after_cancel(self._resize_job)
        self._resize_job = self.win.after(120, self._render)

    def _render(self) -> None:
        self._resize_job = None
        if self.pil is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        pad = SPACE_XL if min(cw, ch) > 300 else SPACE_S
        iw, ih = self.pil.size
        scale = min((cw - 2 * pad) / iw, (ch - 2 * pad) / ih, 1.0)
        size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
        img = self.pil if size == self.pil.size else self.pil.resize(size, Image.BILINEAR)
        self.tkimg = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self.tkimg, anchor="center")
        note = getattr(self, "note", "")
        if note:
            video = bool(self.photo and self.photo.is_video)
            font = self.app.font_small
            tw = font.measure(note) + (40 if video else 24)
            self.canvas.create_image(pad, ch - pad, image=self.app.pill_image(tw, 26), anchor="sw")
            x = pad + 12
            if video:
                self.canvas.create_image(x, ch - pad - 13, image=self.app.icons.get("play", 12, "white"), anchor="w")
                x += 16
            self.canvas.create_text(x, ch - pad - 13, text=note, fill="#FFFFFF", anchor="w", font=font)
        self._draw_nav(cw, ch)

    def open_external(self) -> None:
        p = self.photo
        if p is None:
            return
        for mp in (p.image_path, p.original_path):
            path = self.app.library.existing(self.app.library.to_linux(mp))
            if path:
                try:
                    subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError as exc:
                    messagebox.showerror("Ouverture impossible", f"Impossible de lancer l'application par défaut :\n{exc}", parent=self.win)
                return
        messagebox.showwarning("Fichier introuvable", "Le fichier de cette photo est introuvable sur le disque.", parent=self.win)


class LibraryDialog:
    """Fenêtre de choix d'une bibliothèque : récentes, détectées sur les disques, ou parcourir."""

    def __init__(self, app: "MainWindow"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Ouvrir une bibliothèque — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.transient(app.root)
        self.win.geometry("900x500")
        self.win.minsize(640, 360)
        self.results: "queue.Queue[list]" = queue.Queue()
        self.cancel = threading.Event()
        self.paths: dict[str, tuple[str, str]] = {}   # iid -> (chemin, type)

        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        head.pack(side="top", fill="x")
        ttk.Label(head, image=app.icons.get("library", 28, "accent"), style="Panel.TLabel").pack(
            side="left", padx=(0, SPACE_M), anchor="n")
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text="Choisissez la bibliothèque à explorer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(txt, text=f"Bibliothèque actuelle : {app.library.root}", style="Muted.TLabel").pack(anchor="w")

        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        btns = foot
        self.scan_label = ttk.Label(btns, text="", style="Footer.TLabel")
        self.scan_label.pack(side="left")
        self.btn_open = ttk.Button(btns, text="Ouvrir", style="Accent.TButton", command=self.open_selected)
        self.btn_open.pack(side="right")
        ttk.Button(btns, text="Annuler", command=self.close).pack(side="right", padx=(0, SPACE_S))
        ttk.Button(btns, text="Parcourir…", image=app.icons.get("folder_open", 16), compound="left",
                   command=self.browse).pack(side="right", padx=(0, SPACE_S))
        self.btn_scan = ttk.Button(btns, text="Rechercher sur les disques", image=app.icons.get("search", 16),
                                   compound="left", command=self.scan)
        self.btn_scan.pack(side="right", padx=(0, SPACE_S))

        body = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, 0, SPACE_XL, SPACE_M))
        body.pack(side="top", fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=("path", "state"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Bibliothèque", anchor="w")
        self.tree.heading("path", text="Emplacement", anchor="w")
        self.tree.heading("state", text="État", anchor="w")
        self.tree.column("#0", width=240)
        self.tree.column("path", width=300)
        self.tree.column("state", width=270)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("section", font=app.font_bold, foreground=COLORS["text"])
        self.tree.tag_configure("current", foreground=COLORS["success"])
        self.tree.tag_configure("disabled", foreground=COLORS["faint"])
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.tree.insert("", "end", iid="recent", text=" Récentes", open=True, tags=("section",),
                         image=app.icons.get("refresh", 16, "accent"))
        self.tree.insert("", "end", iid="found", text=" Détectées sur les disques", open=True, tags=("section",),
                         image=app.icons.get("search", 16, "accent"))
        for p in app.settings.recent:
            self._add("recent", p, library_kind(p) or LIB_IPHOTO)
        if not app.settings.recent:
            self._add("recent", app.library.root, LIB_IPHOTO)
        self.win.after(50, self.scan)
        self.win.after(100, self._grab)

    def _grab(self) -> None:
        try:
            self.win.grab_set()
        except tk.TclError:
            pass

    def _add(self, parent: str, path: str, kind: str) -> None:
        iid = f"{parent}:{len(self.tree.get_children(parent))}"
        name = os.path.basename(path.rstrip("/")) or path
        name = re.sub(r"\.photo(s)?library$", "", name)
        tags = ()
        if kind == LIB_PHOTOS_NEW:
            tags = ("disabled",)
        elif os.path.abspath(path) == self.app.library.root:
            tags = ("current",)
            name += "  (actuelle)"
        self.tree.insert(parent, "end", iid=iid, text=" " + name, image=self.app.icons.get("library", 16),
                         values=(os.path.dirname(path), library_status(path)), tags=tags)
        self.paths[iid] = (path, kind)

    def scan(self) -> None:
        self.btn_scan.state(["disabled"])
        self.scan_label.config(text="Recherche des bibliothèques en cours…")
        self.tree.delete(*self.tree.get_children("found"))
        self.paths = {k: v for k, v in self.paths.items() if not k.startswith("found:")}

        def run():
            try:
                self.results.put(find_libraries(cancel=self.cancel))
            except Exception as exc:   # la recherche est facultative
                sys.stderr.write(f"[recherche] {exc}\n")
                self.results.put([])

        threading.Thread(target=run, daemon=True, name="recherche-bibliotheques").start()
        self.win.after(100, self._poll)

    def _poll(self) -> None:
        if not self.win.winfo_exists():
            return
        try:
            found = self.results.get_nowait()
        except queue.Empty:
            self.win.after(150, self._poll)
            return
        for path, kind in found:
            self._add("found", path, kind)
        n = sum(1 for _p, k in found if k in (LIB_IPHOTO, LIB_PHOTOS))
        self.scan_label.config(text=f"{plural(n, 'bibliothèque lisible trouvée', 'bibliothèques lisibles trouvées')}"
                               if found else "Aucune bibliothèque trouvée : utilisez « Parcourir… ».")
        self.btn_scan.state(["!disabled"])
        first = next((iid for iid, (p, k) in self.paths.items()
                      if k in (LIB_IPHOTO, LIB_PHOTOS) and os.path.abspath(p) != self.app.library.root), None)
        if first and not self.tree.selection():
            self.tree.selection_set(first)
            self.tree.focus(first)

    def browse(self) -> None:
        start = os.path.dirname(self.app.library.root) if os.path.isdir(os.path.dirname(self.app.library.root)) \
            else os.path.expanduser("~")
        path = filedialog.askdirectory(parent=self.win, initialdir=start, mustexist=True,
                                       title="Sélectionnez le dossier de la bibliothèque iPhoto ou Photos")
        if path:
            self._try_open(path)

    def open_selected(self) -> None:
        sel = self.tree.selection()
        if not sel or sel[0] not in self.paths:
            messagebox.showinfo("Ouvrir", "Sélectionnez une bibliothèque dans la liste.", parent=self.win)
            return
        self._try_open(self.paths[sel[0]][0])

    def _try_open(self, path: str) -> None:
        kind = library_kind(path)
        if kind == LIB_PHOTOS_NEW:
            messagebox.showwarning(
                "Format non pris en charge",
                "Ce dossier est une bibliothèque de l'application Photos 5 ou plus récente "
                "(macOS 10.15 et suivants, base Photos.sqlite).\n\n"
                "iphotor lit les bibliothèques iPhoto et celles des versions 1 à 4 de Photos "
                "(macOS 10.10 à 10.14).", parent=self.win)
            return
        if kind is None:
            if not os.path.exists(path):
                msg = f"Ce dossier est inaccessible :\n{path}\n\nLe disque est peut-être débranché."
            else:
                msg = (f"Ce dossier ne contient pas de bibliothèque iPhoto ou Photos :\n{path}\n\n"
                       "Ni AlbumData.xml ni base Photos n'a été trouvé. Sélectionnez le dossier de la "
                       "bibliothèque elle-même (par exemple « iPhoto Library » ou « Ma photothèque.photolibrary »).")
            messagebox.showerror("Bibliothèque introuvable", msg, parent=self.win)
            return
        self.close()
        self.app.switch_library(path)

    def close(self) -> None:
        self.cancel.set()
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()


class _Dialog:
    """Base des petites fenêtres modales : en-tête (icône, titre, sous-titre), corps, pied à boutons."""

    def __init__(self, app: "MainWindow", parent, title: str, heading: str = "", subtitle: str = "",
                 icon: str = ""):
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=COLORS["panel"])
        self.win.transient(parent)
        self.win.resizable(True, False)
        if heading:
            head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, 0))
            head.pack(side="top", fill="x")
            if icon:
                ttk.Label(head, image=app.icons.get(icon, 28, "accent"), style="Panel.TLabel").pack(
                    side="left", padx=(0, SPACE_M), anchor="n")
            txt = ttk.Frame(head, style="Panel.TFrame")
            txt.pack(side="left", fill="x", expand=True)
            ttk.Label(txt, text=heading, style="Title.TLabel").pack(anchor="w")
            if subtitle:
                ttk.Label(txt, text=subtitle, style="Muted.TLabel").pack(anchor="w")
        self.footer = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        self.footer.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        self.body = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_L))
        self.body.pack(fill="both", expand=True)

    def buttons(self, ok_text: str, ok_cmd: Callable, cancel_text: str = "Annuler") -> None:
        """Bouton principal (bleu) à droite, bouton secondaire à sa gauche."""
        ttk.Button(self.footer, text=ok_text, style="Accent.TButton", command=ok_cmd).pack(side="right")
        ttk.Button(self.footer, text=cancel_text, command=self.win.destroy).pack(side="right", padx=(0, SPACE_S))

    def _grab(self) -> None:
        try:
            self.win.wait_visibility()
            self.win.grab_set()
        except tk.TclError:
            pass
        self.win.focus_force()


class TagDialog(_Dialog):
    """Choix des tags d'une photo : tags existants (cases) et nouveaux tags."""

    def __init__(self, app: "MainWindow", parent, photo: Photo, current: list[str], known: list[str]):
        super().__init__(app, parent, "Tags — iphotor", "Tags de la photo",
                         safe_text(photo.filename or photo.label), icon="tag")
        self.result: Optional[list[str]] = None
        b = self.body
        self.vars: dict[str, tk.BooleanVar] = {}
        names = sorted(set(known) | set(current), key=normalize_text)
        if names:
            ttk.Label(b, text="TAGS EXISTANTS", style="Section.TLabel").pack(anchor="w")
            box = ttk.Frame(b, style="Panel.TFrame")
            box.pack(anchor="w", fill="x", pady=(SPACE_XS, SPACE_L))
            cols = 3
            for i, name in enumerate(names):
                var = tk.BooleanVar(value=name in current)
                self.vars[name] = var
                ttk.Checkbutton(box, text=name, variable=var).grid(row=i // cols, column=i % cols,
                                                                   sticky="w", padx=(0, 18), pady=1)
        ttk.Label(b, text="NOUVEAUX TAGS", style="Section.TLabel").pack(anchor="w")
        self.new_var = tk.StringVar()
        entry = ttk.Entry(b, textvariable=self.new_var, width=48)
        entry.pack(anchor="w", fill="x", pady=(SPACE_XS, 2))
        ttk.Label(b, text="Séparez plusieurs tags par des virgules.", style="Small.TLabel").pack(anchor="w")
        self.buttons("Enregistrer", self.ok)
        self.win.bind("<Return>", lambda e: self.ok())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._grab()
        entry.focus_set()
        self.win.wait_window()

    def ok(self) -> None:
        tags = [name for name, var in self.vars.items() if var.get()]
        tags += [t for t in (clean_tag(x) for x in self.new_var.get().split(",")) if t]
        self.result = tags
        self.win.destroy()


class ExifDialog:
    """Fenêtre des informations EXIF d'une photo (non modale)."""

    def __init__(self, app: "MainWindow", parent, photo: Photo, path: str, data: dict):
        self.app = app
        self.data = data
        self.path = path
        self.win = tk.Toplevel(parent)
        self.win.title(f"Informations — {photo.filename or photo.label}")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("680x640")
        self.win.minsize(440, 320)
        top = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        top.pack(side="top", fill="x")
        ttk.Label(top, image=app.icons.get("info", 28, "accent"), style="Panel.TLabel").pack(
            side="left", padx=(0, SPACE_M), anchor="n")
        txt = ttk.Frame(top, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text=safe_text(photo.filename or photo.label), style="Title.TLabel").pack(anchor="w")
        try:
            size = format_size(os.path.getsize(path))
        except OSError:
            size = "?"
        ttk.Label(txt, text=f"{safe_text(path)}   ·   {size}", style="Muted.TLabel",
                  wraplength=560, justify="left").pack(anchor="w")

        btns = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        btns.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Button(btns, text="Fermer", style="Accent.TButton", command=self.win.destroy).pack(side="right")
        ttk.Button(btns, text="Copier le texte", image=app.icons.get("copy", 16), compound="left",
                   command=self.copy_text).pack(side="right", padx=(0, SPACE_S))
        if data.get("gps"):
            ttk.Button(btns, text="Voir sur une carte", image=app.icons.get("external", 16), compound="left",
                       command=self.open_map).pack(side="left")

        frame = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_L, 0, SPACE_L, SPACE_S))
        frame.pack(side="top", fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("v",), show="tree", selectmode="browse", style="Item.Treeview")
        self.tree.column("#0", width=230, stretch=False)
        self.tree.column("v", width=380, stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("section", font=app.font_bold, foreground=COLORS["text"])
        self.tree.tag_configure("odd", background=COLORS["footer"])
        self.tree.tag_configure("error", foreground=COLORS["missing"])

        if data.get("error"):
            self.tree.insert("", "end", text=" " + data["error"], tags=("error",),
                             image=app.icons.get("warning", 16, "missing"))
        if data.get("summary"):
            sec = self.tree.insert("", "end", text="Résumé", tags=("section",), open=True)
            for i, (label, value) in enumerate(data["summary"]):
                self.tree.insert(sec, "end", text=label, values=(value,), tags=("odd",) if i % 2 else ())
        for group, items in data.get("groups", []):
            sec = self.tree.insert("", "end", text=f"{group} ({len(items)})", tags=("section",), open=False)
            for i, (name, value) in enumerate(items):
                self.tree.insert(sec, "end", text=name, values=(value,), tags=("odd",) if i % 2 else ())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.focus_force()

    def as_text(self) -> str:
        lines = [self.path, ""]
        if self.data.get("error"):
            lines.append(self.data["error"])
        for label, value in self.data.get("summary", []):
            lines.append(f"{label} : {value}")
        for group, items in self.data.get("groups", []):
            lines += ["", f"[{group}]"] + [f"{n} : {v}" for n, v in items]
        return "\n".join(lines)

    def copy_text(self) -> None:
        self.win.clipboard_clear()
        self.win.clipboard_append(self.as_text())
        self.app._set_status("Informations copiées dans le presse-papiers (texte).")

    def open_map(self) -> None:
        lat, lon = self.data["gps"]
        url = f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=15/{lat:.6f}/{lon:.6f}"
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Carte", f"Impossible d'ouvrir le navigateur :\n{exc}", parent=self.win)


class CopyDialog(_Dialog):
    """Choix de la destination d'une copie."""

    def __init__(self, app: "MainWindow", parent, what: str, summary: str, subdir_name: str,
                 is_library: bool):
        super().__init__(app, parent, "Copier vers un dossier — iphotor", f"Copier {what}", summary,
                         icon="folder_arrow")
        self.result: Optional[dict] = None
        st = app.settings
        b = self.body
        ttk.Label(b, text="DOSSIER DE DESTINATION", style="Section.TLabel").pack(anchor="w")
        row = ttk.Frame(b, style="Panel.TFrame")
        row.pack(fill="x", pady=(SPACE_XS, SPACE_M))
        default = st.copy_dest if st.copy_dest and os.path.isdir(st.copy_dest) else ""
        self.dest_var = tk.StringVar(value=default)
        self.entry = ttk.Entry(row, textvariable=self.dest_var, width=56)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Parcourir…", image=app.icons.get("folder_open", 16), compound="left",
                   command=self.browse).pack(side="left", padx=(SPACE_S, 0))
        self.sub_var = tk.BooleanVar(value=bool(subdir_name))
        self.subdir_name = subdir_name
        if subdir_name:
            ttk.Checkbutton(b, text=f"Créer un sous-dossier « {safe_filename(subdir_name)} »",
                            variable=self.sub_var).pack(anchor="w")
        self.orig_var = tk.BooleanVar(value=st.copy_original)
        if is_library:
            ttk.Label(b, text="FICHIER COPIÉ", style="Section.TLabel").pack(anchor="w", pady=(SPACE_M, 2))
            ttk.Radiobutton(b, text="Version affichée (la version retouchée si elle existe)",
                            variable=self.orig_var, value=False).pack(anchor="w")
            ttk.Radiobutton(b, text="Fichier original (tel que sorti de l'appareil, RAW compris)",
                            variable=self.orig_var, value=True).pack(anchor="w")
        self.open_var = tk.BooleanVar(value=st.copy_open)
        ttk.Checkbutton(b, text="Ouvrir le dossier de destination à la fin",
                        variable=self.open_var).pack(anchor="w", pady=(8, 0))
        note = ttk.Frame(b, style="Panel.TFrame")
        note.pack(anchor="w", fill="x", pady=(SPACE_M, 0))
        ttk.Label(note, image=app.icons.get("lock", 14, "muted"), style="Panel.TLabel").pack(side="left", anchor="n",
                                                                                          pady=(2, 0))
        ttk.Label(note, text="Les fichiers existants ne sont jamais remplacés : un numéro est ajouté au nom "
                             "si besoin.\nLes fichiers d'origine ne sont ni modifiés ni déplacés.",
                  style="Small.TLabel", justify="left").pack(side="left", padx=(SPACE_S, 0))
        self.buttons("Copier", self.ok)
        self.win.bind("<Return>", lambda e: self.ok())
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._grab()
        if not default:
            self.win.after(50, self.browse)
        self.win.wait_window()

    def browse(self) -> None:
        start = self.dest_var.get() or self.app.settings.copy_dest
        if not start or not os.path.isdir(start):
            start = os.path.expanduser("~")
        path = filedialog.askdirectory(parent=self.win, title="Dossier de destination",
                                       initialdir=start, mustexist=True)
        if path:
            self.dest_var.set(path)

    def ok(self) -> None:
        dest = os.path.expanduser(self.dest_var.get().strip())
        if not dest:
            messagebox.showwarning("Destination", "Choisissez un dossier de destination.", parent=self.win)
            return
        if not os.path.isdir(dest):
            messagebox.showerror("Destination", f"Ce dossier n'existe pas :\n{dest}", parent=self.win)
            return
        dest = os.path.abspath(dest)
        st = self.app.settings
        st.copy_dest, st.copy_original, st.copy_open = dest, bool(self.orig_var.get()), bool(self.open_var.get())
        st.save()
        self.result = {"dest": dest, "subdir": self.subdir_name if self.sub_var.get() else "",
                       "original": bool(self.orig_var.get()), "open": bool(self.open_var.get())}
        self.win.destroy()


class CopyProgress:
    """Suivi d'une copie en cours (non modal : la navigation reste possible)."""

    def __init__(self, app: "MainWindow", parent, task: CopyTask, title: str, open_dir: Optional[str]):
        self.app = app
        self.task = task
        self.open_dir = open_dir
        self.title = title
        self.win = tk.Toplevel(parent)
        self.win.title("Copie en cours — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, 0))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get("copy", 28, "accent"), style="Panel.TLabel").pack(
            side="left", padx=(0, SPACE_M), anchor="n")
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(txt, text=f"vers {safe_text(task.dest)}", style="Muted.TLabel").pack(anchor="w")
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        b = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_L))
        b.pack(fill="both", expand=True)
        self.bar = ttk.Progressbar(b, mode="determinate", length=440, maximum=max(1, task.total))
        self.bar.pack(fill="x")
        self.label = ttk.Label(b, text="Préparation…", style="Muted.TLabel")
        self.label.pack(anchor="w", pady=(SPACE_S, 0))
        self.btn = ttk.Button(foot, text="Annuler", command=self.cancel)
        self.btn.pack(side="right")
        threading.Thread(target=task.run, daemon=True, name="copie").start()
        self.win.after(100, self._poll)

    def cancel(self) -> None:
        self.task.cancelled.set()
        self.btn.state(["disabled"])
        self.label.config(text="Annulation…")

    def _poll(self) -> None:
        t = self.task
        if not self.win.winfo_exists():
            return
        if t.finished:
            self._finish()
            return
        if not t.cancelled.is_set():
            if t.phase == "prepare":
                self.bar.configure(maximum=max(1, t.total), value=t.done + len(t.missing))
                self.label.config(text=f"Recherche des fichiers… {format_count(t.done)} / {format_count(t.total)}")
            else:
                self.bar.configure(maximum=max(1, t.total_bytes), value=t.bytes_done)
                self.label.config(text=f"{format_count(t.done)} / {format_count(t.total)} fichiers   ·   "
                                       f"{format_size(t.bytes_done)} sur {format_size(t.total_bytes)}")
        self.win.after(100, self._poll)

    def _finish(self) -> None:
        t = self.task
        self.win.destroy()
        if t.fatal:
            messagebox.showerror("Copie impossible", t.fatal, parent=self.app.root)
            self.app._set_status("Copie interrompue : " + t.fatal.splitlines()[0])
            return
        lines = [f"{plural(t.copied, 'fichier copié', 'fichiers copiés')} dans :\n{safe_text(t.dest)}"]
        if t.cancelled.is_set():
            lines.insert(0, "Copie annulée.")
        if t.missing:
            lines.append(f"\n{plural(len(t.missing), 'fichier introuvable', 'fichiers introuvables')} "
                         "(disque débranché ou fichier absent) :\n  " + "\n  ".join(t.missing[:8])
                         + ("\n  …" if len(t.missing) > 8 else ""))
        if t.failed:
            lines.append(f"\n{plural(len(t.failed), 'erreur')} :\n  " + "\n  ".join(t.failed[:8])
                         + ("\n  …" if len(t.failed) > 8 else ""))
        self.app._set_status(f"{self.title} : {plural(t.copied, 'fichier copié', 'fichiers copiés')}"
                             + (f", {len(t.missing)} introuvable(s)" if t.missing else "")
                             + (f", {len(t.failed)} erreur(s)" if t.failed else "") + ".")
        if t.missing or t.failed or t.cancelled.is_set():
            messagebox.showwarning("Copie terminée", "\n".join(lines), parent=self.app.root)
        elif t.copied > 1:
            messagebox.showinfo("Copie terminée", "\n".join(lines), parent=self.app.root)
        if self.open_dir and t.copied and os.path.isdir(self.open_dir):
            try:
                subprocess.Popen(["xdg-open", self.open_dir], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass


class TaskDialog:
    """Progression d'une opération longue exécutée dans un thread (non modale, annulable).

    work(task) s'exécute dans un thread ; il met à jour task.done / task.total / task.text et
    surveille task.cancel. done(result, error) est appelée dans le thread Tk à la fin.
    """

    def __init__(self, app: "MainWindow", parent, title: str, heading: str, work: Callable, done: Callable,
                 icon: str = "refresh", cancellable: bool = True):
        self.app = app
        self.cancel = threading.Event()
        self.done_cb = done
        self.total = 0
        self.done = 0
        self.text = "Préparation…"
        self._result: list = []
        self.win = tk.Toplevel(parent)
        self.win.title(f"{title} — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.request_cancel)
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, 0))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get(icon, 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M))
        ttk.Label(head, text=heading, style="Title.TLabel", wraplength=420, justify="left").pack(side="left", anchor="w")
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        body = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L))
        body.pack(fill="both", expand=True)
        self.bar = ttk.Progressbar(body, mode="indeterminate", length=440)
        self.bar.pack(fill="x")
        self.bar.start(12)
        self.label = ttk.Label(body, text=self.text, style="Muted.TLabel", wraplength=440, justify="left")
        self.label.pack(anchor="w", pady=(SPACE_S, 0))
        self.btn = ttk.Button(foot, text="Annuler", command=self.request_cancel)
        if cancellable:
            self.btn.pack(side="right")

        def runner():
            try:
                self._result.append((work(self), None))
            except Exception as exc:
                traceback.print_exc()
                self._result.append((None, exc))
        threading.Thread(target=runner, daemon=True, name="tache").start()
        self.win.after(120, self._poll)

    def request_cancel(self) -> None:
        self.cancel.set()
        self.btn.state(["disabled"])
        self.label.config(text="Annulation…")

    def _poll(self) -> None:
        if not self.win.winfo_exists():
            return
        if self._result:
            result, error = self._result[0]
            self.win.destroy()
            self.done_cb(result, error)
            return
        if self.total and str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        if self.total:
            self.bar.configure(maximum=max(1, self.total), value=self.done)
        if not self.cancel.is_set():
            self.label.config(text=self.text)
        self.win.after(120, self._poll)


class TextDialog(_Dialog):
    """Saisie d'un texte (nom d'album, de recherche…)."""

    def __init__(self, app, parent, title: str, heading: str, label: str, initial: str = "", icon: str = "album",
                 ok_text: str = "Valider"):
        super().__init__(app, parent, f"{title} — iphotor", heading, icon=icon)
        self.result: Optional[str] = None
        ttk.Label(self.body, text=label.upper(), style="Section.TLabel").pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        e = ttk.Entry(self.body, textvariable=self.var, width=44)
        e.pack(fill="x", pady=(SPACE_XS, 0))
        e.select_range(0, "end")
        self.buttons(ok_text, self.ok)
        self.win.bind("<Return>", lambda ev: self.ok())
        self.win.bind("<Escape>", lambda ev: self.win.destroy())
        self._grab()
        e.focus_set()
        self.win.wait_window()

    def ok(self) -> None:
        v = self.var.get().strip()
        if not v:
            messagebox.showwarning("Nom vide", "Saisissez un nom.", parent=self.win)
            return
        self.result = v
        self.win.destroy()


class AlbumChooserDialog(_Dialog):
    """Choix d'un album virtuel existant, ou création d'un nouvel album."""

    def __init__(self, app, parent, count: int):
        super().__init__(app, parent, "Ajouter à un album virtuel — iphotor", "Ajouter à un album virtuel",
                         f"{plural(count, 'photo')} : seules des références sont enregistrées, les fichiers ne "
                         "bougent pas.", icon="album")
        self.result: Optional[int] = None
        self.albums = app.collections.albums("manual")
        ttk.Label(self.body, text="ALBUM", style="Section.TLabel").pack(anchor="w")
        self.var = tk.StringVar(value="__new__" if not self.albums else str(self.albums[0].id))
        box = ttk.Frame(self.body, style="Panel.TFrame")
        box.pack(fill="x", pady=(SPACE_XS, SPACE_S))
        for a in self.albums[:14]:
            ttk.Radiobutton(box, text=f"{a.name}  ({plural(a.count, 'photo')})", value=str(a.id),
                            variable=self.var).pack(anchor="w")
        row = ttk.Frame(box, style="Panel.TFrame")
        row.pack(anchor="w", fill="x")
        ttk.Radiobutton(row, text="Nouvel album :", value="__new__", variable=self.var).pack(side="left")
        self.name = tk.StringVar(value="")
        e = ttk.Entry(row, textvariable=self.name, width=28)
        e.pack(side="left", padx=(SPACE_S, 0), fill="x", expand=True)
        e.bind("<FocusIn>", lambda ev: self.var.set("__new__"))
        self.buttons("Ajouter", self.ok)
        self.win.bind("<Return>", lambda ev: self.ok())
        self.win.bind("<Escape>", lambda ev: self.win.destroy())
        self._grab()
        if not self.albums:
            e.focus_set()
        self.win.wait_window()

    def ok(self) -> None:
        if self.var.get() == "__new__":
            name = self.name.get().strip()
            if not name:
                messagebox.showwarning("Nom vide", "Saisissez le nom du nouvel album.", parent=self.win)
                return
            try:
                self.result = self.app.collections.create(name)
            except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
                messagebox.showerror("Album virtuel", str(exc), parent=self.win)
                return
        else:
            self.result = int(self.var.get())
        self.win.destroy()


class InfoDialog:
    """Fiche d'informations (clé / valeur), non modale, avec copie du texte."""

    def __init__(self, app, parent, title: str, heading: str, subtitle: str, rows: list, icon: str = "info"):
        self.app = app
        self.rows = rows
        self.win = tk.Toplevel(parent)
        self.win.title(f"{title} — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("640x520")
        self.win.minsize(420, 300)
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get(icon, 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M), anchor="n")
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text=heading, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(txt, text=subtitle, style="Muted.TLabel", wraplength=520, justify="left").pack(anchor="w")
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Button(foot, text="Fermer", style="Accent.TButton", command=self.win.destroy).pack(side="right")
        ttk.Button(foot, text="Copier le texte", image=app.icons.get("copy", 16), compound="left",
                   command=self.copy).pack(side="right", padx=(0, SPACE_S))
        frame = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_L, 0, SPACE_L, SPACE_S))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("v",), show="tree", style="Item.Treeview")
        self.tree.column("#0", width=230, stretch=False)
        self.tree.column("v", width=360, stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("section", font=app.font_bold, foreground=COLORS["text"])
        self.tree.tag_configure("warn", foreground=COLORS["missing"])
        parent_iid = ""
        for row in rows:
            if row[0] == "#":
                parent_iid = self.tree.insert("", "end", text=row[1], tags=("section",), open=True)
            else:
                tags = ("warn",) if len(row) > 2 and row[2] == "warn" else ()
                self.tree.insert(parent_iid, "end", text=row[0], values=(row[1],), tags=tags)
        self.win.bind("<Escape>", lambda e: self.win.destroy())

    def copy(self) -> None:
        lines = []
        for row in self.rows:
            lines.append(f"\n[{row[1]}]" if row[0] == "#" else f"{row[0]} : {row[1]}")
        self.win.clipboard_clear()
        self.win.clipboard_append("\n".join(lines).strip())
        self.app._set_status("Informations copiées dans le presse-papiers.")


class AdvancedSearchDialog:
    """Recherche avancée multicritère (fenêtre non modale, une seule instance)."""

    def __init__(self, app: "MainWindow", criteria: Optional[dict] = None, smart_id: Optional[int] = None):
        self.app = app
        self.smart_id = smart_id
        self.dialect = app.search_dialect()
        self.win = tk.Toplevel(app.root)
        self.win.title("Recherche avancée — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("900x560")
        self.win.minsize(720, 380)
        self.win.transient(app.root)
        self.rows: list[dict] = []
        self.fields = [f for f in advsearch.FIELDS if f.expr(self.dialect) is not None]
        self.unavailable = [f for f in advsearch.FIELDS if f.expr(self.dialect) is None]
        self.labels = [f"{f.group} · {f.label}" for f in self.fields]
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_S))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get("search", 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M), anchor="n")
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text="Modifier l'album intelligent" if smart_id else "Recherche avancée",
                  style="Title.TLabel").pack(anchor="w")
        self.coverage = ttk.Label(txt, text="", style="Muted.TLabel", wraplength=640, justify="left")
        self.coverage.pack(anchor="w")
        self.btn_analyze = ttk.Button(head, text="Analyser les métadonnées…", image=app.icons.get("refresh", 16),
                                      compound="left", command=self._analyze)
        self.btn_analyze.pack(side="right", anchor="n")
        # combinaison
        mrow = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, 0, SPACE_XL, SPACE_S))
        mrow.pack(fill="x")
        ttk.Label(mrow, text="Afficher les photos qui remplissent", style="Panel.TLabel").pack(side="left")
        self.match = tk.StringVar(value="all")
        ttk.Radiobutton(mrow, text="tous les critères (ET)", value="all", variable=self.match).pack(side="left", padx=(SPACE_M, 0))
        ttk.Radiobutton(mrow, text="au moins un critère (OU)", value="any", variable=self.match).pack(side="left", padx=(SPACE_M, 0))
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        self.result_label = ttk.Label(foot, text="", style="Footer.TLabel")
        self.result_label.pack(side="left")
        ttk.Button(foot, text="Rechercher" if not smart_id else "Enregistrer", style="Accent.TButton",
                   command=self.apply if not smart_id else self.save_smart).pack(side="right")
        ttk.Button(foot, text="Fermer", command=self.win.destroy).pack(side="right", padx=(0, SPACE_S))
        if not smart_id:
            mb = ttk.Menubutton(foot, text="Enregistrer…", style="TButton")
            m = tk.Menu(mb, tearoff=False)
            m.add_command(label="Enregistrer comme recherche…", command=self.save_search)
            m.add_command(label="Créer un album intelligent…", command=self.create_smart)
            mb["menu"] = m
            mb.pack(side="right", padx=(0, SPACE_S))
            self.saved_mb = ttk.Menubutton(foot, text="Recherches enregistrées", style="TButton")
            self.saved_menu = tk.Menu(self.saved_mb, tearoff=False, postcommand=self._fill_saved)
            self.saved_mb["menu"] = self.saved_menu
            self.saved_mb.pack(side="right", padx=(0, SPACE_S))
        ttk.Button(foot, text="Réinitialiser", command=self.reset).pack(side="right", padx=(0, SPACE_S))
        # critères (zone défilante)
        outer = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, 0, SPACE_L, SPACE_S))
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, bg=COLORS["panel"], highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        self.add_btn = ttk.Button(self.inner, text="Ajouter un critère", image=app.icons.get("search", 16),
                                  compound="left", style="Ghost.TButton", command=lambda: self.add_rule())
        if self.unavailable:
            names = ", ".join(f.label.lower() for f in self.unavailable)
            self.na_label = ttk.Label(self.inner, text=f"Non disponibles pour cette source : {names}.",
                                      style="Small.TLabel", wraplength=780, justify="left")
        else:
            self.na_label = None
        self.load(criteria or app.adv_criteria or {"match": "all", "rules": [{"field": "filename", "op": "contains", "value": ""}]})
        self.refresh_coverage()
        self.win.bind("<Return>", lambda e: self.apply() if not smart_id else self.save_smart())
        self.win.bind("<Escape>", lambda e: self.win.destroy())

    # -- critères ------------------------------------------------------------------
    def load(self, criteria: dict) -> None:
        for r in list(self.rows):
            r["frame"].destroy()
        self.rows = []
        self.match.set(criteria.get("match", "all"))
        for rule in criteria.get("rules", []) or [{}]:
            self.add_rule(rule)

    def add_rule(self, rule: Optional[dict] = None) -> None:
        rule = rule or {}
        fr = ttk.Frame(self.inner, style="Panel.TFrame")
        fr.pack(fill="x", pady=3, before=self.add_btn if self.add_btn.winfo_ismapped() else None)
        r = {"frame": fr, "field": tk.StringVar(), "op": tk.StringVar(), "value": tk.StringVar(),
             "value2": tk.StringVar()}
        fbox = ttk.Combobox(fr, textvariable=r["field"], values=self.labels, state="readonly", width=28)
        fbox.pack(side="left")
        obox = ttk.Combobox(fr, textvariable=r["op"], state="readonly", width=14)
        obox.pack(side="left", padx=(SPACE_S, 0))
        val = ttk.Combobox(fr, textvariable=r["value"], width=14)
        val.pack(side="left", padx=(SPACE_S, 0), fill="x", expand=True)
        and_lbl = ttk.Label(fr, text="et", style="Muted.TLabel")
        val2 = ttk.Entry(fr, textvariable=r["value2"], width=12)
        unit = ttk.Label(fr, text="", style="Muted.TLabel")
        rm = ttk.Button(fr, image=self.app.icons.get("close", 14, "muted"), style="Icon.TButton",
                        command=lambda: self._remove(r))
        rm.pack(side="right", padx=(SPACE_S, 0))
        Tooltip(rm, "Retirer ce critère")
        r.update(fbox=fbox, obox=obox, val=val, val2=val2, and_lbl=and_lbl, unit=unit)
        self.rows.append(r)
        name = rule.get("field") or "filename"
        f = advsearch.FIELD_BY_NAME.get(name)
        if f not in self.fields:
            f = self.fields[0]
        r["field"].set(self.labels[self.fields.index(f)])
        self._field_changed(r, rule.get("op"))
        if rule.get("value") is not None:
            v = rule["value"]
            if f.type == "choice":
                v = dict(f.choices).get(v, v)
            r["value"].set(str(v))
        if rule.get("value2") is not None:
            r["value2"].set(str(rule["value2"]))
        fbox.bind("<<ComboboxSelected>>", lambda e: (fbox.selection_clear(), self._field_changed(r)))
        obox.bind("<<ComboboxSelected>>", lambda e: (obox.selection_clear(), self._op_changed(r)))
        val.bind("<<ComboboxSelected>>", lambda e: val.selection_clear())
        self.add_btn.pack_forget()
        self.add_btn.pack(anchor="w", pady=(SPACE_S, 0))
        if self.na_label is not None:
            self.na_label.pack_forget()
            self.na_label.pack(anchor="w", pady=(SPACE_M, 0))

    def _remove(self, r: dict) -> None:
        r["frame"].destroy()
        self.rows.remove(r)

    def _field(self, r: dict) -> "advsearch.Field":
        lbl = r["field"].get()
        return self.fields[self.labels.index(lbl)] if lbl in self.labels else self.fields[0]

    def _field_changed(self, r: dict, op: Optional[str] = None) -> None:
        f = self._field(r)
        ops = advsearch.OPS[f.type]
        r["obox"].configure(values=[lbl for _o, lbl in ops])
        code = op if op in dict(ops) else ops[0][0]
        r["op"].set(dict(ops)[code])
        if f.type == "choice":
            r["val"].configure(values=[lbl for _v, lbl in f.choices], state="readonly")
            r["value"].set(f.choices[0][1])
        elif f.type == "tag":
            r["val"].configure(values=self.app.known_tags(), state="normal")
            r["value"].set("")
        else:
            r["val"].configure(values=(), state="normal")
            r["value"].set("")
        r["unit"].configure(text=f.unit)
        self._op_changed(r)

    def _op_code(self, r: dict) -> str:
        f = self._field(r)
        return next((o for o, lbl in advsearch.OPS[f.type] if lbl == r["op"].get()), advsearch.OPS[f.type][0][0])

    def _op_changed(self, r: dict) -> None:
        code = self._op_code(r)
        f = self._field(r)
        for w in (r["and_lbl"], r["val2"], r["unit"]):
            w.pack_forget()
        if code in advsearch.NO_VALUE_OPS:
            r["val"].pack_forget()
            return
        if not r["val"].winfo_ismapped():
            r["val"].pack(side="left", padx=(SPACE_S, 0), fill="x", expand=True, after=r["obox"])
        last = r["val"]
        if code == "between":
            r["and_lbl"].pack(side="left", padx=(SPACE_S, 0), after=r["val"])
            r["val2"].pack(side="left", padx=(SPACE_S, 0), after=r["and_lbl"])
            last = r["val2"]
        if f.unit:
            r["unit"].pack(side="left", padx=(SPACE_XS, 0), after=last)

    def criteria(self) -> dict:
        rules = []
        for r in self.rows:
            f = self._field(r)
            code = self._op_code(r)
            rule = {"field": f.name, "op": code}
            if code not in advsearch.NO_VALUE_OPS:
                v = r["value"].get().strip()
                if f.type == "choice":
                    v = next((c for c, lbl in f.choices if lbl == v), v)
                rule["value"] = v
                if code == "between":
                    rule["value2"] = r["value2"].get().strip()
            rules.append(rule)
        return {"match": self.match.get(), "rules": rules}

    def reset(self) -> None:
        self.load({"match": "all", "rules": [{"field": "filename", "op": "contains"}]})
        self.result_label.config(text="")

    # -- actions -------------------------------------------------------------------
    def refresh_coverage(self) -> None:
        total, analysed, with_exif = self.app.metadata_coverage()
        if total == 0:
            self.coverage.config(text="Aucune photo dans la source courante.")
        elif analysed < total:
            self.coverage.config(text=f"Métadonnées analysées : {format_count(analysed)} sur {format_count(total)} "
                                      "photos. Les critères marqués « Photo » (appareil, dimensions, ISO, GPS…) "
                                      "ne peuvent retenir que des photos analysées.")
        else:
            self.coverage.config(text=f"Métadonnées analysées pour les {format_count(total)} photos "
                                      f"({format_count(with_exif)} avec EXIF).")

    def _analyze(self) -> None:
        self.app.analyze_metadata(parent=self.win, done=lambda: self.win.winfo_exists() and self.refresh_coverage())

    def apply(self) -> None:
        crit = self.criteria()
        try:
            advsearch.compile_criteria(crit, self.dialect, self.app.annotation_source())
        except advsearch.CriteriaError as exc:
            messagebox.showwarning("Recherche avancée", str(exc), parent=self.win)
            return
        self.result_label.config(text="Recherche en cours…")
        self.app.run_advanced_search(crit, title="Recherche avancée",
                                     done=lambda n, warns: self.win.winfo_exists() and self.result_label.config(
                                         text=f"{plural(n, 'photo trouvée', 'photos trouvées')}"
                                              + (f" — {warns[0]}" if warns else "")))

    def _fill_saved(self) -> None:
        m = self.saved_menu
        m.delete(0, "end")
        saved = self.app.collections.searches()
        if not saved:
            m.add_command(label="(aucune recherche enregistrée)", state="disabled")
        for sid, name, crit in saved:
            m.add_command(label=name, command=lambda c=crit: self.load(c))
        if saved:
            m.add_separator()
            sub = tk.Menu(m, tearoff=False)
            for sid, name, _c in saved:
                sub.add_command(label=name, command=lambda i=sid, n=name: self._delete_saved(i, n))
            m.add_cascade(label="Supprimer", menu=sub)

    def _delete_saved(self, sid: int, name: str) -> None:
        if messagebox.askyesno("Supprimer", f"Supprimer la recherche enregistrée « {name} » ?", parent=self.win):
            self.app.collections.delete_search(sid)
            self.app.refresh_smart_entries()

    def _validated(self) -> Optional[dict]:
        crit = self.criteria()
        try:
            advsearch.compile_criteria(crit, self.dialect, self.app.annotation_source())
        except advsearch.CriteriaError as exc:
            messagebox.showwarning("Recherche avancée", str(exc), parent=self.win)
            return None
        return crit

    def save_search(self) -> None:
        crit = self._validated()
        if crit is None:
            return
        d = TextDialog(self.app, self.win, "Enregistrer la recherche", "Enregistrer la recherche",
                       "Nom de la recherche", advsearch.describe(crit)[:60], icon="search", ok_text="Enregistrer")
        if d.result:
            self.app.collections.save_search(d.result, crit)
            self.app.refresh_smart_entries()
            self.app._set_status(f"Recherche « {d.result} » enregistrée.")

    def create_smart(self) -> None:
        crit = self._validated()
        if crit is None:
            return
        d = TextDialog(self.app, self.win, "Album intelligent", "Créer un album intelligent",
                       "Nom de l'album", advsearch.describe(crit)[:60], icon="smart", ok_text="Créer")
        if d.result:
            self.app.collections.create(d.result, "smart", crit)
            self.app.refresh_smart_entries()
            self.app._set_status(f"Album intelligent « {d.result} » créé : il se met à jour à chaque ouverture.")

    def save_smart(self) -> None:
        crit = self._validated()
        if crit is None:
            return
        self.app.collections.set_criteria(self.smart_id, crit)
        self.app.refresh_smart_entries()
        self.win.destroy()
        self.app.open_smart(self.smart_id)


SCOPE_LABELS = (("selection", "La sélection"), ("view", "La vue affichée"), ("source", "Toute la source"))


class ExportDialog(_Dialog):
    """Export des métadonnées en CSV ou JSON."""

    def __init__(self, app: "MainWindow", parent, scope: str = ""):
        super().__init__(app, parent, "Exporter les métadonnées — iphotor", "Exporter les métadonnées",
                         "CSV (tableur) ou JSON. Le fichier est écrit uniquement à l'emplacement choisi.",
                         icon="file")
        b = self.body
        ttk.Label(b, text="PHOTOS À EXPORTER", style="Section.TLabel").pack(anchor="w")
        self.scope = tk.StringVar(value=scope or ("selection" if app.selection else "view"))
        row = ttk.Frame(b, style="Panel.TFrame")
        row.pack(anchor="w", pady=(SPACE_XS, SPACE_M))
        for code, label in SCOPE_LABELS:
            n = app.scope_count(code)
            rb = ttk.Radiobutton(row, text=f"{label} ({format_count(n)})", value=code, variable=self.scope)
            rb.pack(side="left", padx=(0, SPACE_L))
            if n == 0:
                rb.state(["disabled"])
        ttk.Label(b, text="FORMAT", style="Section.TLabel").pack(anchor="w")
        self.fmt = tk.StringVar(value="csv")
        row = ttk.Frame(b, style="Panel.TFrame")
        row.pack(anchor="w", pady=(SPACE_XS, SPACE_M))
        ttk.Radiobutton(row, text="CSV (séparateur « ; », UTF-8)", value="csv", variable=self.fmt).pack(side="left")
        ttk.Radiobutton(row, text="JSON", value="json", variable=self.fmt).pack(side="left", padx=(SPACE_L, 0))
        hdr = ttk.Frame(b, style="Panel.TFrame")
        hdr.pack(fill="x")
        ttk.Label(hdr, text="CHAMPS", style="Section.TLabel").pack(side="left")
        ttk.Button(hdr, text="Aucun", style="Ghost.TButton", command=lambda: self._all(False)).pack(side="right")
        ttk.Button(hdr, text="Tous", style="Ghost.TButton", command=lambda: self._all(True)).pack(side="right")
        grid = ttk.Frame(b, style="Panel.TFrame")
        grid.pack(fill="x", pady=(SPACE_XS, 0))
        self.vars = {}
        for i, (key, label) in enumerate(mexport.FIELDS):
            v = tk.BooleanVar(value=True)
            self.vars[key] = v
            ttk.Checkbutton(grid, text=label, variable=v).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, SPACE_L))
        ttk.Label(b, text="Les champs EXIF ne sont remplis que pour les photos dont les métadonnées ont été "
                          "analysées (Outils › Analyser les métadonnées).", style="Small.TLabel",
                  wraplength=560, justify="left").pack(anchor="w", pady=(SPACE_M, 0))
        self.buttons("Exporter…", self.ok)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._grab()

    def _all(self, on: bool) -> None:
        for v in self.vars.values():
            v.set(on)

    def ok(self) -> None:
        fields = [k for k, _ in mexport.FIELDS if self.vars[k].get()]
        if not fields:
            messagebox.showwarning("Export", "Choisissez au moins un champ.", parent=self.win)
            return
        fmt = self.fmt.get()
        start = self.app.settings.copy_dest if os.path.isdir(self.app.settings.copy_dest or "") else os.path.expanduser("~")
        path = filedialog.asksaveasfilename(parent=self.win, title="Fichier d'export", initialdir=start,
                                            initialfile=f"iphotor-metadonnees.{fmt}", defaultextension=f".{fmt}",
                                            filetypes=[("CSV", "*.csv")] if fmt == "csv" else [("JSON", "*.json")])
        if not path:
            return
        try:
            mexport.check_destination(path, self.app._protected_roots())
        except (OSError, ValueError) as exc:
            messagebox.showerror("Export impossible", str(exc), parent=self.win)
            return
        scope = self.scope.get()
        self.win.destroy()
        self.app.export_metadata(scope, fmt, fields, path)


def draw_bars(c, x: int, y: int, w: int, title: str, data: list, color: str, fonts, max_items: int = 15,
              vertical: bool = False) -> int:
    """Diagramme en barres simple sur un Canvas ; renvoie la hauteur utilisée."""
    c.create_text(x, y, text=title, anchor="nw", fill=COLORS["text"], font=fonts["bold"])
    y0 = y + 26
    data = [(str(k), v) for k, v in data][:max_items] if not vertical else [(str(k), v) for k, v in data]
    if not data or not any(v for _k, v in data):
        c.create_text(x, y0, text="Aucune donnée disponible.", anchor="nw", fill=COLORS["muted"], font=fonts["small"])
        return 56
    vmax = max(v for _k, v in data) or 1
    if vertical:
        h = 150
        n = len(data)
        bw = max(4, min(42, (w - 10) // max(1, n) - 4))
        for i, (k, v) in enumerate(data):
            bh = int((h - 30) * v / vmax)
            bx = x + i * (bw + 4)
            c.create_rectangle(bx, y0 + h - 20 - bh, bx + bw, y0 + h - 20, fill=color, outline="")
            if n <= 24 or i % max(1, n // 12) == 0:
                c.create_text(bx + bw / 2, y0 + h - 16, text=k if len(k) <= 6 else k[:5] + ".", anchor="n",
                              fill=COLORS["muted"], font=fonts["badge"])
            if v and (n <= 16):
                c.create_text(bx + bw / 2, y0 + h - 24 - bh, text=format_count(v), anchor="s",
                              fill=COLORS["muted"], font=fonts["badge"])
        return h + 34
    label_w = 170
    for i, (k, v) in enumerate(data):
        yy = y0 + i * 22
        c.create_text(x, yy + 8, text=(k if len(k) <= 26 else k[:25] + "…"), anchor="w", fill=COLORS["text"],
                      font=fonts["small"])
        bw = int((w - label_w - 70) * v / vmax)
        c.create_rectangle(x + label_w, yy + 2, x + label_w + max(2, bw), yy + 15, fill=color, outline="")
        c.create_text(x + label_w + max(2, bw) + 6, yy + 8, text=format_count(v), anchor="w",
                      fill=COLORS["muted"], font=fonts["small"])
    return 26 + len(data) * 22 + 12


class StatsWindow:
    """Statistiques photographiques (calculées localement)."""

    def __init__(self, app: "MainWindow"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Statistiques — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("980x760")
        self.win.minsize(700, 420)
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get("library", 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M))
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text="Statistiques", style="Title.TLabel").pack(anchor="w")
        self.sub = ttk.Label(txt, text="", style="Muted.TLabel", wraplength=600, justify="left")
        self.sub.pack(anchor="w")
        self.scope = tk.StringVar(value="source")
        box = ttk.Combobox(head, state="readonly", width=22,
                           values=[lbl for code, lbl in SCOPE_LABELS if app.scope_count(code)])
        box.set("Toute la source")
        box.pack(side="right")
        box.bind("<<ComboboxSelected>>", lambda e: (box.selection_clear(), self.compute(
            next(c for c, l in SCOPE_LABELS if l == box.get()))))
        ab = ttk.Button(head, text="Analyser…", image=app.icons.get("refresh", 16), compound="left",
                        command=lambda: app.analyze_metadata(parent=self.win, done=lambda: self.compute(self.scope.get())))
        ab.pack(side="right", padx=(0, SPACE_S))
        Tooltip(ab, "Analyser les métadonnées (EXIF, GPS) des photos qui ne le sont pas encore")
        ttk.Separator(self.win, orient="horizontal").pack(fill="x")
        frame = ttk.Frame(self.win, style="Bg.TFrame")
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, bg=COLORS["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview, style="Grid.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=sb.set, yscrollincrement=20)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for seq, d in (("<Button-4>", -3), ("<Button-5>", 3)):
            self.canvas.bind(seq, lambda e, d=d: self.canvas.yview_scroll(d, "units"))
        self.canvas.bind("<Configure>", lambda e: self.stats and self.render(self.stats))
        self.stats = None
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.compute("source")

    def compute(self, scope: str) -> None:
        self.scope.set(scope)
        self.canvas.delete("all")
        self.canvas.create_text(30, 30, text="Calcul des statistiques…", anchor="nw", fill=COLORS["muted"],
                                font=self.app.font_big)
        t0 = time.time()

        def done(result, error):
            if not self.win.winfo_exists():
                return
            if error is not None:
                self.canvas.delete("all")
                self.canvas.create_text(30, 30, text=f"Calcul impossible : {error}", anchor="nw",
                                        fill=COLORS["missing"], font=self.app.font_big)
                return
            self.stats = result
            self.sub.config(text=f"{dict(SCOPE_LABELS)[scope]} — calculées en {time.time() - t0:.1f} s, "
                                 f"à partir de l'index local (aucun fichier relu).")
            self.render(result)

        self.app.compute_stats(scope, done)

    def render(self, s: dict) -> None:
        c = self.canvas
        c.delete("all")
        f = self.app.fonts
        w = max(600, c.winfo_width())
        pad = SPACE_XL
        y = pad
        tiles = [
            ("Photos et vidéos", format_count(s["total"])), ("Vidéos", format_count(s["videos"])),
            ("Fichiers RAW", format_count(s["raw"])),
            ("Taille totale", statlib.format_bytes(s["size_total"]) + ("" if s["size_known"] == s["total"] else " (connue)")),
            ("Taille moyenne", statlib.format_bytes(s["size_avg"]) if s["size_known"] else "—"),
            ("Géolocalisées", f"{s['gps_pct']:.0f} %".replace(".", ",") if s["analysed"] else "—"),
            ("Sans date exploitable", format_count(s["no_date"])),
            ("Sans EXIF", format_count(s["without_exif"]) if s["analysed"] else "—"),
        ]
        cols = max(2, min(4, (w - 2 * pad) // 210))
        tw = (w - 2 * pad - (cols - 1) * 12) // cols
        for i, (label, value) in enumerate(tiles):
            x0 = pad + (i % cols) * (tw + 12)
            y0 = y + (i // cols) * 84
            c.create_rectangle(x0, y0, x0 + tw, y0 + 72, fill=COLORS["card"], outline=COLORS["card_border"])
            c.create_text(x0 + 14, y0 + 12, text=label, anchor="nw", fill=COLORS["muted"], font=f["small"])
            c.create_text(x0 + 14, y0 + 32, text=value, anchor="nw", fill=COLORS["text"], font=f["title"])
        y += ((len(tiles) + cols - 1) // cols) * 84 + 8
        if s["analysed"] < s["total"]:
            c.create_text(pad, y, text=f"Métadonnées analysées pour {format_count(s['analysed'])} photos sur "
                                       f"{format_count(s['total'])} : appareils, objectifs, ISO, focales, dimensions "
                                       "et GPS ne portent que sur celles-ci.",
                          anchor="nw", fill=COLORS["missing"] if s["analysed"] == 0 else COLORS["muted"],
                          font=f["small"], width=w - 2 * pad)
            y += 34
        acc, sec = COLORS["accent_fill"], COLORS["secondary"]
        y += draw_bars(c, pad, y, w - 2 * pad, "Photos par année", s["years"], acc, f, vertical=True)
        y += draw_bars(c, pad, y, w - 2 * pad, "Photos par mois (toutes années)", s["months"], sec, f, vertical=True)
        half = (w - 2 * pad - 24) // 2
        rows = [("Extensions", s["extensions"], "Dimensions les plus fréquentes", s["dimensions"]),
                ("Appareils les plus utilisés", s["cameras"], "Objectifs les plus utilisés", s["lenses"]),
                ("Sensibilité ISO", s["iso"], "Focales (équivalent 24×36 si connu)", s["focal"])]
        for t1, d1, t2, d2 in rows:
            h1 = draw_bars(c, pad, y, half, t1, d1, acc, f, max_items=10)
            h2 = draw_bars(c, pad + half + 24, y, half, t2, d2, sec, f, max_items=10)
            y += max(h1, h2) + 8
        c.configure(scrollregion=(0, 0, w, y + pad))


class DuplicatesWindow:
    """Résultats de la détection des doublons. Aucune suppression : copie possible des versions retenues."""

    def __init__(self, app: "MainWindow", report: "dupes.Report", saved_at: Optional[float] = None):
        self.app = app
        self.report = report
        self.win = tk.Toplevel(app.root)
        self.win.title("Doublons — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("1180x700")
        self.win.minsize(820, 420)
        self._thumb = None
        self._thumb_req = 0
        self._res: "queue.Queue[tuple]" = queue.Queue()
        counts = report.counts()
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get("copy", 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M))
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text="Doublons et photos proches", style="Title.TLabel").pack(anchor="w")
        when = f" — résultats du {_dt.datetime.fromtimestamp(saved_at).strftime('%d/%m/%Y à %H:%M')}" if saved_at else ""
        ttk.Label(txt, text=f"{format_count(report.analysed)} fichiers analysés : {counts[dupes.IDENTICAL]} groupe(s) "
                            f"identiques, {counts[dupes.NEAR]} quasi-identiques, {counts[dupes.SIMILAR]} proches"
                            f"{when}. Aucun fichier n'est jamais supprimé.",
                  style="Muted.TLabel", wraplength=900, justify="left").pack(anchor="w")
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Button(foot, text="Fermer", style="Accent.TButton", command=self.win.destroy).pack(side="right")
        self.btn_copy = ttk.Button(foot, text="Copier la sélection vers…", image=app.icons.get("folder_arrow", 16),
                                   compound="left", command=self.copy_selected)
        self.btn_copy.pack(side="right", padx=(0, SPACE_S))
        self.btn_cmp = ttk.Button(foot, text="Comparer", image=app.icons.get("library", 16), compound="left",
                                  command=self.compare)
        self.btn_cmp.pack(side="right", padx=(0, SPACE_S))
        self.btn_grid = ttk.Button(foot, text="Afficher le groupe dans la grille", command=self.show_in_grid)
        self.btn_grid.pack(side="left")
        ttk.Label(foot, text="Ctrl+clic : sélectionner plusieurs fichiers.", style="Footer.TLabel").pack(
            side="left", padx=(SPACE_M, 0))
        body = tk.PanedWindow(self.win, orient="horizontal", sashwidth=5, bg=COLORS["panel"], bd=0)
        body.pack(fill="both", expand=True, padx=SPACE_L, pady=SPACE_S)
        left = ttk.Frame(body, style="Panel.TFrame")
        cols = ("loc", "dims", "date", "size", "sim")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="extended", style="Item.Treeview")
        for col, label, w in (("#0", "Fichier", 300), ("loc", "Emplacement", 200), ("dims", "Dimensions", 95),
                              ("date", "Date", 125), ("size", "Taille", 70), ("sim", "Similarité", 120)):
            self.tree.heading(col, text=label, anchor="w")
            self.tree.column(col, width=w, stretch=col in ("#0", "loc"))
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("group", font=app.font_bold)
        self.tree.tag_configure("missing", foreground=COLORS["missing"])
        body.add(left, minsize=480, width=830)
        right = ttk.Frame(body, style="Panel.TFrame", padding=(SPACE_M, 0, 0, 0))
        self.preview = tk.Canvas(right, bg=COLORS["well"], highlightthickness=0, width=300, height=300)
        self.preview.pack(fill="both", expand=True)
        self.info = ttk.Label(right, text="", style="Small.TLabel", wraplength=300, justify="left")
        self.info.pack(anchor="w", pady=(SPACE_S, 0))
        body.add(right, minsize=240)
        self.entries: dict[str, tuple] = {}
        icon = {dupes.IDENTICAL: "copy", dupes.NEAR: "image_off", dupes.SIMILAR: "photos"}
        for gi, g in enumerate(report.groups):
            title = {dupes.IDENTICAL: "Contenu identique", dupes.NEAR: "Quasi-identiques (redimensionnée, recompressée…)",
                     dupes.SIMILAR: "Photos proches — même série possible, pas forcément des doublons"}[g.level]
            gid = self.tree.insert("", "end", iid=f"g{gi}", text=f" {title} · {len(g.entries)} fichiers",
                                   values=("", "", "", "", g.similarity), tags=("group",), open=gi < 30,
                                   image=app.icons.get(icon[g.level], 16, "accent"))
            for ei, e in enumerate(g.entries):
                iid = f"g{gi}e{ei}"
                exists = os.path.isfile(e.path)
                self.tree.insert(gid, "end", iid=iid, text=os.path.basename(e.path),
                                 values=(safe_text(os.path.dirname(e.path)),
                                         f"{e.width} × {e.height}" if e.width else "—",
                                         format_date(storage.unix_to_apple(e.date)) if e.date else "—",
                                         format_size(e.size) if e.size else "—",
                                         "" if g.level == dupes.IDENTICAL or not e.phash or not g.entries[0].phash
                                         else f"{dupes.distance(e.phash, g.entries[0].phash)} bit(s) d'écart"),
                                 tags=() if exists else ("missing",))
                self.entries[iid] = (gi, e)
        if not report.groups:
            self.tree.insert("", "end", text="Aucun doublon ni photo proche trouvé.")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._update_buttons()
        self._poll()

    def _selected_entries(self) -> list:
        return [self.entries[i][1] for i in self.tree.selection() if i in self.entries]

    def _update_buttons(self) -> None:
        sel = self._selected_entries()
        self.btn_cmp.state(["!disabled"] if len(sel) == 2 else ["disabled"])
        self.btn_copy.state(["!disabled"] if sel else ["disabled"])

    def _on_select(self, _e=None) -> None:
        self._update_buttons()
        sel = self.tree.selection()
        if not sel or sel[-1] not in self.entries:
            return
        e = self.entries[sel[-1]][1]
        self.info.config(text=f"{safe_text(e.path)}\n{format_size(e.size) if e.size else ''}"
                              + (f"\nSHA-256 : {e.sha256[:16]}…" if e.sha256 else ""))
        self._thumb_req += 1
        no = self._thumb_req
        w, h = max(200, self.preview.winfo_width()), max(200, self.preview.winfo_height())

        def work():
            try:
                self._res.put((no, load_pil_image(e.path, w - 16, h - 16), None))
            except Exception as exc:
                self._res.put((no, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        if not self.win.winfo_exists():
            return
        try:
            while True:
                no, img, err = self._res.get_nowait()
                if no != self._thumb_req:
                    continue
                self.preview.delete("all")
                w, h = self.preview.winfo_width(), self.preview.winfo_height()
                if img is None:
                    self.preview.create_text(w // 2, h // 2, text=f"Aperçu impossible\n{err}", fill=COLORS["muted"],
                                             justify="center", width=w - 20)
                else:
                    self._thumb = ImageTk.PhotoImage(img)
                    self.preview.create_image(w // 2, h // 2, image=self._thumb)
        except queue.Empty:
            pass
        self.win.after(80, self._poll)

    def compare(self) -> None:
        sel = self._selected_entries()
        if len(sel) == 2:
            CompareWindow(self.app, [(os.path.basename(e.path), e.path, e.key) for e in sel])

    def copy_selected(self) -> None:
        sel = self._selected_entries()
        if not sel:
            return
        photos = [self.app.photo_from_path(e.path) for e in sel]
        self.app._start_copy([(p, "") for p in photos], f"{plural(len(photos), 'fichier retenu', 'fichiers retenus')}",
                             plural(len(photos), "fichier"), "", parent=self.win)

    def show_in_grid(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        gi = int(sel[0][1:].split("e")[0])
        keys = {e.key for e in self.report.groups[gi].entries}
        self.app.show_keys_in_grid(keys, f"Doublons : groupe {gi + 1}")


class CompareWindow:
    """Comparaison de deux photos : côte à côte (zoom et déplacement synchronisés) ou rideau."""

    FIELDS = (("Fichier", "filename"), ("Dimensions", "dims"), ("Date de prise de vue", "taken"),
              ("Taille du fichier", "size"), ("Appareil", "camera"), ("Objectif", "lens"), ("Focale", "focal"),
              ("Ouverture", "aperture"), ("Temps de pose", "exposure"), ("ISO", "iso"), ("GPS", "gps"),
              ("Emplacement", "folder"))

    def __init__(self, app: "MainWindow", items: list):
        """items : deux tuples (libellé, chemin, clé de métadonnées)."""
        self.app = app
        self.items = items[:2]
        self.pils = [None, None]
        self.tk = [None, None]
        self.zoom = 1.0           # 1.0 = image entière visible
        self.center = (0.5, 0.5)  # centre de la vue, en coordonnées relatives de l'image
        self.mode = tk.StringVar(value="side")
        self.split = 0.5
        self._drag = None
        self._job = None
        self.res: "queue.Queue[tuple]" = queue.Queue()
        self.win = tk.Toplevel(app.root)
        self.win.title("Comparer — iphotor")
        self.win.configure(bg=COLORS["panel"])
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        self.win.geometry(f"{int(sw * 0.8)}x{int(sh * 0.82)}")
        self.win.minsize(700, 480)
        bar = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_M, SPACE_S))
        bar.pack(fill="x")
        ttk.Radiobutton(bar, text="Côte à côte", value="side", variable=self.mode, style="Segment.Toolbutton",
                        command=self.render).pack(side="left")
        ttk.Radiobutton(bar, text="Rideau", value="curtain", variable=self.mode, style="Segment.Toolbutton",
                        command=self.render).pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="Ajuster", style="Ghost.TButton", command=self.fit).pack(side="left", padx=(SPACE_M, 0))
        ttk.Button(bar, text="100 %", style="Ghost.TButton", command=self.actual).pack(side="left")
        ttk.Button(bar, text="Inverser", style="Ghost.TButton", command=self.swap).pack(side="left")
        ttk.Label(bar, text="Molette : zoom synchronisé · glisser : déplacer · rideau : glisser la séparation",
                  style="Muted.TLabel").pack(side="right")
        ttk.Separator(self.win, orient="horizontal").pack(fill="x")
        self.table = ttk.Treeview(self.win, columns=("a", "b"), show="tree headings", height=12, style="Item.Treeview")
        self.table.heading("#0", text="", anchor="w")
        self.table.column("#0", width=170, stretch=False)
        self.table.pack(side="bottom", fill="x", padx=SPACE_L, pady=SPACE_S)
        self.table.tag_configure("diff", foreground=COLORS["accent"], font=app.font_bold)
        self.area = ttk.Frame(self.win, style="Panel.TFrame")
        self.area.pack(fill="both", expand=True)
        self.canvases = [tk.Canvas(self.area, bg=COLORS["viewer_bg"], highlightthickness=0) for _ in range(2)]
        for cv in self.canvases:
            cv._viewer = True
            cv.bind("<Configure>", lambda e: self._schedule())
            cv.bind("<Button-4>", lambda e: self._wheel(e, 1.25))
            cv.bind("<Button-5>", lambda e: self._wheel(e, 0.8))
            cv.bind("<MouseWheel>", lambda e: self._wheel(e, 1.25 if e.delta > 0 else 0.8))
            cv.bind("<ButtonPress-1>", self._press)
            cv.bind("<B1-Motion>", self._motion)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self._fill_table()
        for i, (_label, path, _key) in enumerate(self.items):
            threading.Thread(target=self._load, args=(i, path), daemon=True).start()
        self._poll()

    def _load(self, i: int, path: str) -> None:
        try:
            self.res.put((i, load_pil_image(path, 2600, 2600), None))
        except Exception as exc:
            self.res.put((i, None, str(exc)))

    def _poll(self) -> None:
        if not self.win.winfo_exists():
            return
        try:
            while True:
                i, img, err = self.res.get_nowait()
                self.pils[i] = img if img is not None else err
                self.render()
        except queue.Empty:
            pass
        self.win.after(60, self._poll)

    def _meta(self, path: str, key: str) -> dict:
        m = self.app.meta_record(key, path) or {}
        if not m.get("width"):
            try:
                m = {**mdlib.extract(path), **{k: v for k, v in m.items() if v is not None}}
            except Exception:
                pass
        return m

    def _fill_table(self) -> None:
        metas = [self._meta(p, k) for _l, p, k in self.items]
        self.table.heading("a", text=self.items[0][0], anchor="w")
        self.table.heading("b", text=self.items[1][0], anchor="w")

        def val(m, f, path):
            if f == "filename":
                return os.path.basename(path)
            if f == "folder":
                return safe_text(os.path.dirname(path))
            if f == "dims":
                return f"{m['width']} × {m['height']} px" if m.get("width") else "—"
            if f == "taken":
                return format_date(storage.unix_to_apple(m["taken"])) if m.get("taken") else "—"
            if f == "size":
                return format_size(m["file_size"]) if m.get("file_size") else "—"
            if f == "camera":
                mk, md = (m.get("make") or "").strip(), (m.get("model") or "").strip()
                if mk and md.lower().startswith(mk.split()[0].lower()):
                    return md
                return " ".join(x for x in (mk, md) if x) or "—"
            if f == "focal":
                return f"{m['focal']:g} mm" if m.get("focal") else "—"
            if f == "aperture":
                return f"f/{m['aperture']:g}" if m.get("aperture") else "—"
            if f == "exposure":
                e = m.get("exposure")
                return ("—" if not e else f"1/{round(1 / e)} s" if e < 1 else f"{e:g} s")
            if f == "gps":
                return geo.format_coords(m["gps_lat"], m["gps_lon"]) if m.get("gps_lat") is not None else "—"
            v = m.get(f)
            return "—" if v in (None, "") else str(v)
        for label, f in self.FIELDS:
            a, b = val(metas[0], f, self.items[0][1]), val(metas[1], f, self.items[1][1])
            self.table.insert("", "end", text=label, values=(a, b),
                              tags=("diff",) if a != b and f not in ("filename", "folder") else ())

    def fit(self) -> None:
        self.zoom, self.center = 1.0, (0.5, 0.5)
        self.render()

    def actual(self) -> None:
        img = self.pils[0] if not isinstance(self.pils[0], (str, type(None))) else self.pils[1]
        if img is None or isinstance(img, str):
            return
        cv = self.canvases[0]
        fit = min(cv.winfo_width() / img.width, cv.winfo_height() / img.height)
        self.zoom = max(1.0, 1.0 / fit) if fit < 1 else 1.0
        self.render()

    def swap(self) -> None:
        self.items.reverse()
        self.pils.reverse()
        self.table.delete(*self.table.get_children())
        self._fill_table()
        self.render()

    def _wheel(self, e, factor: float) -> None:
        self.zoom = max(1.0, min(16.0, self.zoom * factor))
        if self.zoom == 1.0:
            self.center = (0.5, 0.5)
        self._schedule()

    def _press(self, e) -> None:
        self._drag = (e.x, e.y, self.center, e.widget)
        if self.mode.get() == "curtain":
            self.split = min(0.98, max(0.02, e.x / max(1, e.widget.winfo_width())))
            self.render()

    def _motion(self, e) -> None:
        if self._drag is None:
            return
        if self.mode.get() == "curtain" and self.zoom == 1.0:
            self.split = min(0.98, max(0.02, e.x / max(1, e.widget.winfo_width())))
            self._schedule(10)
            return
        x0, y0, (cx, cy), cv = self._drag
        img = next((p for p in self.pils if p is not None and not isinstance(p, str)), None)
        if img is None:
            return
        scale = self._scale(img, cv)
        self.center = (min(1, max(0, cx - (e.x - x0) / (img.width * scale))),
                       min(1, max(0, cy - (e.y - y0) / (img.height * scale))))
        self._schedule(15)

    def _scale(self, img, cv) -> float:
        return min(cv.winfo_width() / img.width, cv.winfo_height() / img.height, 1.0) * self.zoom

    def _schedule(self, delay: int = 60) -> None:
        if self._job:
            self.win.after_cancel(self._job)
        self._job = self.win.after(delay, self.render)

    def _view(self, img, cv):
        """Partie visible de l'image (zoom et centre partagés), redimensionnée pour le canevas."""
        cw, ch = max(cv.winfo_width(), 10), max(cv.winfo_height(), 10)
        s = self._scale(img, cv)
        vw, vh = min(img.width, cw / s), min(img.height, ch / s)
        cx, cy = self.center[0] * img.width, self.center[1] * img.height
        x0 = min(max(0, cx - vw / 2), img.width - vw)
        y0 = min(max(0, cy - vh / 2), img.height - vh)
        crop = img.crop((int(x0), int(y0), int(x0 + vw), int(y0 + vh)))
        size = (max(1, int(crop.width * s)), max(1, int(crop.height * s)))
        return crop.resize(size, Image.BILINEAR if s < 2 else Image.NEAREST)

    def render(self) -> None:
        self._job = None
        side = self.mode.get() == "side"
        for cv in self.canvases:
            cv.pack_forget()
        if side:
            self.canvases[0].pack(side="left", fill="both", expand=True, padx=(0, 1))
            self.canvases[1].pack(side="left", fill="both", expand=True)
        else:
            self.canvases[0].pack(fill="both", expand=True)
        self.win.update_idletasks()
        for i, cv in enumerate(self.canvases if side else self.canvases[:1]):
            cv.delete("all")
        if side:
            for i, cv in enumerate(self.canvases):
                self._draw(cv, i)
        else:
            self._draw_curtain(self.canvases[0])

    def _draw(self, cv, i: int) -> None:
        cw, ch = cv.winfo_width(), cv.winfo_height()
        img = self.pils[i]
        if img is None or isinstance(img, str):
            cv.create_text(cw // 2, ch // 2, text="Chargement…" if img is None else f"Image illisible\n{img}",
                           fill=COLORS["viewer_text"], justify="center", width=cw - 40)
        else:
            self.tk[i] = ImageTk.PhotoImage(self._view(img, cv))
            cv.create_image(cw // 2, ch // 2, image=self.tk[i])
        cv.create_text(12, 12, text=self.items[i][0], anchor="nw", fill="#FFFFFF", font=self.app.font_bold)

    def _draw_curtain(self, cv) -> None:
        cw, ch = cv.winfo_width(), cv.winfo_height()
        imgs = [p for p in self.pils if p is not None and not isinstance(p, str)]
        if len(imgs) < 2:
            cv.create_text(cw // 2, ch // 2, text="Chargement…", fill=COLORS["viewer_text"])
            return
        a = self._view(self.pils[0], cv)
        b = self._view(self.pils[1], cv).resize(a.size, Image.BILINEAR)
        cut = int(a.width * self.split)
        comp = a.copy()
        comp.paste(b.crop((cut, 0, b.width, b.height)), (cut, 0))
        self.tk[0] = ImageTk.PhotoImage(comp)
        x0 = (cw - a.width) // 2
        cv.create_image(cw // 2, ch // 2, image=self.tk[0])
        cv.create_line(x0 + cut, 0, x0 + cut, ch, fill="#FFFFFF", width=2)
        cv.create_text(x0 + cut - 10, 14, text=self.items[0][0], anchor="ne", fill="#FFFFFF", font=self.app.font_bold)
        cv.create_text(x0 + cut + 10, 14, text=self.items[1][0], anchor="nw", fill="#FFFFFF", font=self.app.font_bold)


class GeoWindow:
    """Exploration géographique hors ligne (coordonnées GPS EXIF ; lieux Apple s'ils existent)."""

    def __init__(self, app: "MainWindow", points: list, missing: int):
        self.app = app
        self.points = points
        self.box = geo.bounds(points)
        self.clusters: list = []
        self.selected: Optional[int] = None
        self.win = tk.Toplevel(app.root)
        self.win.title("Carte — iphotor")
        self.win.configure(bg=COLORS["panel"])
        self.win.geometry("1180x720")
        self.win.minsize(820, 460)
        head = ttk.Frame(self.win, style="Panel.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_XL, SPACE_M))
        head.pack(fill="x")
        ttk.Label(head, image=app.icons.get("external", 28, "accent"), style="Panel.TLabel").pack(side="left", padx=(0, SPACE_M))
        txt = ttk.Frame(head, style="Panel.TFrame")
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text="Photos géolocalisées", style="Title.TLabel").pack(anchor="w")
        ttk.Label(txt, text=f"{format_count(len(points))} photos avec coordonnées GPS"
                            + (f" ; {format_count(missing)} photos pas encore analysées" if missing else "")
                            + ". Carte locale (points et graticule) : aucune donnée n'est envoyée sur Internet.",
                  style="Muted.TLabel", wraplength=780, justify="left").pack(anchor="w")
        self.level = tk.StringVar(value="secteur")
        box = ttk.Combobox(head, state="readonly", width=20, values=[lbl for _n, _s, lbl in geo.LEVELS])
        box.set(dict((n, l) for n, _s, l in geo.LEVELS)["secteur"])
        box.pack(side="right")
        box.bind("<<ComboboxSelected>>", lambda e: (box.selection_clear(), self.level.set(
            next(n for n, _s, l in geo.LEVELS if l == box.get())), self.rebuild()))
        ttk.Label(head, text="Regrouper par", style="Muted.TLabel").pack(side="right", padx=(0, SPACE_S))
        foot = ttk.Frame(self.win, style="Footer.TFrame", padding=(SPACE_XL, SPACE_M))
        foot.pack(side="bottom", fill="x")
        ttk.Separator(self.win, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Button(foot, text="Fermer", style="Accent.TButton", command=self.win.destroy).pack(side="right")
        ttk.Button(foot, text="Ouvrir dans OpenStreetMap…", image=app.icons.get("external", 16), compound="left",
                   command=self.open_osm).pack(side="right", padx=(0, SPACE_S))
        ttk.Button(foot, text="Copier les coordonnées", command=self.copy_coords).pack(side="right", padx=(0, SPACE_S))
        ttk.Button(foot, text="Afficher les photos dans la grille", image=app.icons.get("photos", 16), compound="left",
                   command=self.show_grid).pack(side="left")
        self.coord = ttk.Label(foot, text="", style="Footer.TLabel")
        self.coord.pack(side="left", padx=(SPACE_M, 0))
        body = tk.PanedWindow(self.win, orient="horizontal", sashwidth=5, bg=COLORS["panel"], bd=0)
        body.pack(fill="both", expand=True, padx=SPACE_L, pady=SPACE_S)
        left = ttk.Frame(body, style="Panel.TFrame")
        self.tree = ttk.Treeview(left, columns=("n",), show="tree headings", selectmode="browse", style="Item.Treeview")
        self.tree.heading("#0", text="Lieu", anchor="w")
        self.tree.heading("n", text="Photos", anchor="e")
        self.tree.column("n", width=70, anchor="e", stretch=False)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree)
        body.add(left, minsize=260, width=360)
        self.map = tk.Canvas(body, bg=COLORS["well"], highlightthickness=0)
        self.map.bind("<Configure>", lambda e: self.draw())
        self.map.bind("<Motion>", self._hover)
        self.map.bind("<Button-1>", self._click)
        body.add(self.map, minsize=400)
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.rebuild()

    def rebuild(self) -> None:
        self.clusters = geo.clusters(self.points, self.level.get())
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(self.clusters):
            node = self.tree.insert("", "end", iid=f"c{i}", text=" " + c.name, values=(format_count(len(c.points)),),
                                    image=self.app.icons.get("dot", 12, "accent"))
            if c.places and len(c.places) > 1:
                for name, n in sorted(c.places.items(), key=lambda kv: -kv[1])[:8]:
                    self.tree.insert(node, "end", text=name, values=(format_count(n),))
        self.selected = None
        self.draw()

    def draw(self) -> None:
        c = self.map
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50:
            return
        lat0, lat1, lon0, lon1 = self.box
        step = geo.grid_step(max(lat1 - lat0, lon1 - lon0))
        import math as _m
        lat = _m.floor(lat0 / step) * step
        while lat <= lat1:
            x0, y = geo.project(lat, lon0, self.box, w, h)
            x1, _ = geo.project(lat, lon1, self.box, w, h)
            c.create_line(x0, y, x1, y, fill=COLORS["card_border"])
            c.create_text(x0 + 4, y - 2, text=f"{lat:g}°", anchor="sw", fill=COLORS["faint"], font=self.app.font_badge)
            lat += step
        lon = _m.floor(lon0 / step) * step
        while lon <= lon1:
            x, y0 = geo.project(lat0, lon, self.box, w, h)
            _, y1 = geo.project(lat1, lon, self.box, w, h)
            c.create_line(x, y0, x, y1, fill=COLORS["card_border"])
            c.create_text(x + 3, y0 - 3, text=f"{lon:g}°", anchor="sw", fill=COLORS["faint"], font=self.app.font_badge)
            lon += step
        if not self.points:
            c.create_text(w // 2, h // 2, text="Aucune photo géolocalisée.\nAnalysez les métadonnées (Outils) si ce "
                                               "n'est pas encore fait.", fill=COLORS["muted"], justify="center",
                          font=self.app.font_big)
            return
        for p in self.points[:20000]:
            x, y = geo.project(p.lat, p.lon, self.box, w, h)
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=COLORS["secondary"], outline="")
        boxes: list = []
        order = sorted(range(min(400, len(self.clusters))), key=lambda i: (i != self.selected, i))
        for i in order:
            cl = self.clusters[i]
            x, y = geo.project(cl.lat, cl.lon, self.box, w, h)
            r = 6 + min(18, 3 * len(cl.points) ** 0.5)
            sel = i == self.selected
            c.create_oval(x - r, y - r, x + r, y + r, fill=COLORS["accent_fill"] if sel else "",
                          outline=COLORS["accent"], width=3 if sel else 2, tags=(f"c{i}",))
            label = f"{cl.name} ({len(cl.points)})"
            tw = self.app.font_small.measure(label)
            bx = (x + r + 4, y - 9, x + r + 8 + tw, y + 9)
            if (sel or i < 30) and not any(bx[0] < b[2] and b[0] < bx[2] and bx[1] < b[3] and b[1] < bx[3]
                                           for b in boxes):
                boxes.append(bx)
                if sel:
                    c.create_rectangle(*bx, fill=COLORS["panel"], outline=COLORS["accent"])
                c.create_text(x + r + 6, y, text=label, anchor="w", fill=COLORS["text"],
                              font=self.app.font_bold if sel else self.app.font_small)

    def _hover(self, e) -> None:
        if self.points:
            lat, lon = geo.unproject(e.x, e.y, self.box, self.map.winfo_width(), self.map.winfo_height())
            self.coord.config(text=geo.format_coords(lat, lon))

    def _click(self, e) -> None:
        items = self.map.find_overlapping(e.x - 3, e.y - 3, e.x + 3, e.y + 3)
        for it in reversed(items):
            for t in self.map.gettags(it):
                if t.startswith("c") and t[1:].isdigit():
                    self.tree.selection_set(t)
                    self.tree.see(t)
                    return

    def _on_tree(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0] if sel[0].startswith("c") else self.tree.parent(sel[0])
        try:
            self.selected = int(iid[1:])
        except ValueError:
            return
        cl = self.clusters[self.selected]
        self.coord.config(text=f"{cl.name} — centre {geo.format_coords(cl.lat, cl.lon)} — {plural(len(cl.points), 'photo')}")
        self.draw()

    def _cluster(self):
        if self.selected is None:
            messagebox.showinfo("Carte", "Sélectionnez d'abord un lieu dans la liste ou sur la carte.", parent=self.win)
            return None
        return self.clusters[self.selected]

    def show_grid(self) -> None:
        cl = self._cluster()
        if cl:
            self.app.show_keys_in_grid({p.key for p in cl.points}, f"Lieu : {cl.name}")

    def copy_coords(self) -> None:
        cl = self._cluster()
        if cl:
            self.win.clipboard_clear()
            self.win.clipboard_append(f"{cl.lat:.6f}, {cl.lon:.6f}")
            self.app._set_status("Coordonnées copiées.")

    def open_osm(self) -> None:
        cl = self._cluster()
        if not cl:
            return
        if not messagebox.askyesno("Ouvrir OpenStreetMap",
                                   "Le navigateur va ouvrir openstreetmap.org à cette position :\n"
                                   f"{geo.format_coords(cl.lat, cl.lon)}\n\nLes coordonnées seront donc envoyées à ce "
                                   "site. Continuer ?", parent=self.win):
            return
        try:
            subprocess.Popen(["xdg-open", geo.osm_url(cl.lat, cl.lon)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("Carte", f"Impossible d'ouvrir le navigateur :\n{exc}", parent=self.win)


class SlideshowWindow:
    """Diaporama plein écran (images adaptées à l'écran, chargées dans un thread)."""

    def __init__(self, app: "MainWindow", photos: list, start: int = 0):
        self.app = app
        st = app.settings
        self.photos = [p for p in photos if not p.is_video] or photos
        if st.slideshow_order == "random":
            import random
            self.photos = self.photos[:]
            random.shuffle(self.photos)
        self.index = max(0, min(start, len(self.photos) - 1))
        self.playing = True
        self.interval = st.slideshow_interval
        self.loop = st.slideshow_loop
        self.show_info = st.slideshow_info
        self.cache: dict[int, object] = {}
        self.res: "queue.Queue[tuple]" = queue.Queue()
        self._timer = None
        self._tk = None
        self.win = tk.Toplevel(app.root)
        self.win.title("Diaporama — iphotor")
        self.win.configure(bg="#000000")
        self.win.attributes("-fullscreen", True)
        self.canvas = tk.Canvas(self.win, bg="#000000", highlightthickness=0, cursor="none")
        self.canvas.pack(fill="both", expand=True)
        self.sw, self.sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        b = self.win.bind
        b("<Escape>", lambda e: self.close())
        b("<space>", lambda e: self.toggle())
        b("<Right>", lambda e: self.step(1))
        b("<Left>", lambda e: self.step(-1))
        b("<plus>", lambda e: self.set_interval(self.interval + 1))
        b("<KP_Add>", lambda e: self.set_interval(self.interval + 1))
        b("<minus>", lambda e: self.set_interval(self.interval - 1))
        b("<KP_Subtract>", lambda e: self.set_interval(self.interval - 1))
        b("<KeyPress-i>", lambda e: self.toggle_info())
        b("<KeyPress-l>", lambda e: self.toggle_loop())
        b("<KeyPress-f>", lambda e: self.win.attributes("-fullscreen", not self.win.attributes("-fullscreen")))
        self.canvas.bind("<Button-1>", lambda e: self.step(1))
        self.canvas.bind("<Button-3>", lambda e: self.step(-1))
        self.win.focus_force()
        self._message = ""
        self._poll()
        self.show()

    def _load(self, i: int) -> None:
        p = self.photos[i]
        lib = self.app.library
        try:
            path, _o = lib.find_display_file(p, for_thumbnail=False)
            if not path:
                raise FileNotFoundError("fichier introuvable")
            self.res.put((i, load_pil_image(path, self.sw, self.sh), None))
        except Exception as exc:
            self.res.put((i, None, str(exc)))

    def _request(self, i: int) -> None:
        if 0 <= i < len(self.photos) and i not in self.cache:
            self.cache[i] = None
            threading.Thread(target=self._load, args=(i,), daemon=True).start()

    def _poll(self) -> None:
        if not self.win.winfo_exists():
            return
        try:
            while True:
                i, img, err = self.res.get_nowait()
                self.cache[i] = img if img is not None else err
                if i == self.index:
                    self.render()
        except queue.Empty:
            pass
        self.win.after(50, self._poll)

    def show(self) -> None:
        for k in [k for k in self.cache if abs(k - self.index) > 2]:
            del self.cache[k]           # au plus 5 images en mémoire
        self._request(self.index)
        self._request(self.index + 1)
        self.render()
        self._arm()

    def _arm(self) -> None:
        if self._timer:
            self.win.after_cancel(self._timer)
            self._timer = None
        if self.playing:
            self._timer = self.win.after(self.interval * 1000, self._tick)

    def _tick(self) -> None:
        self._timer = None
        if self.index >= len(self.photos) - 1 and not self.loop:
            self.playing = False
            self._flash("Fin du diaporama — Échap pour quitter")
            return
        self.step(1)

    def step(self, d: int) -> None:
        n = len(self.photos)
        if not n:
            return
        j = self.index + d
        if self.loop:
            j %= n
        self.index = max(0, min(n - 1, j))
        self.show()

    def toggle(self) -> None:
        self.playing = not self.playing
        self._flash("Lecture" if self.playing else "Pause")
        self._arm()

    def set_interval(self, v: int) -> None:
        self.interval = max(1, min(60, v))
        self.app.settings.slideshow_interval = self.interval
        self.app.settings.save()
        self._flash(f"Intervalle : {self.interval} s")
        self._arm()

    def toggle_info(self) -> None:
        self.show_info = not self.show_info
        self.app.settings.slideshow_info = self.show_info
        self.app.settings.save()
        self.render()

    def toggle_loop(self) -> None:
        self.loop = not self.loop
        self.app.settings.slideshow_loop = self.loop
        self.app.settings.save()
        self._flash("Boucle activée" if self.loop else "Boucle désactivée")

    def _flash(self, text: str) -> None:
        self._message = text
        self.render()
        self.win.after(1500, lambda: (setattr(self, "_message", ""), self.render()) if self.win.winfo_exists() else None)

    def render(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width() or self.sw, c.winfo_height() or self.sh
        img = self.cache.get(self.index)
        if img is None:
            c.create_text(w // 2, h // 2, text="Chargement…", fill="#888888", font=self.app.font_big)
        elif isinstance(img, str):
            c.create_text(w // 2, h // 2, text=f"Image indisponible\n{img}", fill="#cc6666", font=self.app.font_big,
                          justify="center")
        else:
            self._tk = ImageTk.PhotoImage(img)
            c.create_image(w // 2, h // 2, image=self._tk)
        if self.show_info and self.photos:
            p = self.photos[self.index]
            info = f"{p.label}   ·   {self.index + 1} / {len(self.photos)}"
            if p.date is not None:
                info += f"   ·   {format_date(p.date)}"
            c.create_text(24, h - 24, text=info, anchor="sw", fill="#DDDDDD", font=self.app.font)
            c.create_text(w - 24, h - 24, anchor="se", fill="#777777", font=self.app.font_small,
                          text=f"{'▶' if self.playing else '❚❚'} {self.interval} s · Espace pause · ←/→ · +/- "
                               f"intervalle · I infos · L boucle{' (activée)' if self.loop else ''} · Échap")
        if self._message:
            c.create_text(w // 2, 60, text=self._message, fill="#FFFFFF", font=self.app.font_title)

    def close(self) -> None:
        if self._timer:
            self.win.after_cancel(self._timer)
        self.cache.clear()
        self.win.destroy()


class MainWindow:
    """Fenêtre principale : arborescence, grille de vignettes, recherche."""

    def __init__(self, root: "tk.Tk", library: LibraryManager, cache: CacheManager,
                 settings: Optional[Settings] = None, force_reindex: bool = False):
        self.root = root
        self.library = library
        self.cache = cache
        self.settings = settings or Settings()
        self.con: Optional[sqlite3.Connection] = None
        self.albums: Optional[AlbumManager] = None
        self.photos: Optional[PhotoManager] = None
        self.loader = ThumbnailLoader(library, cache)
        self.viewer: Optional[PhotoViewer] = None

        self.view = View("empty")
        self.total = 0
        self.page_no = 0
        self.page_photos: list[Photo] = []
        self.generation = 0
        self.tile_images: dict[int, object] = {}
        self.view_ids: Optional[list[int]] = None
        self.hover_index: Optional[int] = None
        self._layout_job = None
        self._building = False
        self._build_queue: "queue.Queue[tuple]" = queue.Queue()
        self.current_album_id: Optional[int] = None
        self._last_grid_msg: tuple[str, str] = ("", "")
        self.current_index: Optional[int] = None   # photo affichée dans la visionneuse (index absolu)

        # Mode « Explorer un dossier »
        self.mode = MODE_LIBRARY
        self._mode_states: dict[str, dict] = {}
        self.folder = FolderBrowser()
        self._scanner: Optional[FolderScanner] = None
        self._scan_queue: "queue.Queue[tuple]" = queue.Queue()
        self._scan_polling = False

        # Favoris et tags (hors bibliothèque)
        self.annotations = Annotations()
        self._annot_sel = None          # "fav", ("tag", nom) ou None
        self._tag_names: list[str] = []
        self._tree_silent = False

        # iphotor 2
        self.selection: dict[str, Photo] = {}
        self._sel_anchor: Optional[int] = None
        self.adv_criteria: Optional[dict] = None
        self._adv_views: dict = {}
        self._adv_dialog = None
        self.collections = vcoll.Collections(protected=self._protected_roots)
        self.coll_browser: Optional[CollectionBrowser] = None
        self.current_valbum: Optional[int] = None
        self._timeline = None
        self._timeline_key = None
        self._tl_unknown: list[int] = []
        self._tl_stack: list = []
        self.periods: list = []
        self._stats_cache: dict = {}
        self._disk_ok: Optional[bool] = None
        self._watch_count = 0
        self._watch_busy = False
        self._folder_sig: Optional[dict] = None

        self._setup_style()
        self._build_ui()
        self._update_title()
        self.root.after(30, self._poll_thumbnails)
        self.root.after(100, lambda: self.start(force_reindex))
        self.root.after(5000, self._watch_tick)

    # -- style ---------------------------------------------------------------
    def _setup_style(self) -> None:
        root = self.root
        COLORS.clear()
        COLORS.update(PALETTES.get(self.settings.theme, PALETTES["light"]))
        root.configure(bg=COLORS["bg"])
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1500, int(sw * 0.9))}x{min(950, int(sh * 0.88))}")
        root.minsize(900, 560)

        self.fonts = setup_fonts(root)
        f = self.fonts
        self.font = f["base"]
        self.font_small = f["small"]
        self.font_bold = f["bold"]
        self.font_title = f["title"]
        self.font_big = f["big"]
        self.font_badge = f["badge"]
        self.icons = Icons(root)
        self.theme = Theme(root, f)
        self.theme.apply()
        self._custom_images: dict[str, tuple[object, Callable]] = {}
        try:
            root.iconphoto(True, ImageTk.PhotoImage(draw_logo(64, PALETTES["light"]["accent_fill"]), master=root))
        except tk.TclError:
            pass

    # -- images composées (vignettes, badges, surimpressions) ----------------------------
    def _custom(self, key: str, build: Callable):
        """Image mise en cache ; build() est rappelée lors d'un changement de thème."""
        entry = self._custom_images.get(key)
        if entry is None:
            entry = (ImageTk.PhotoImage(build(), master=self.root), build)
            self._custom_images[key] = entry
        return entry[0]

    def card_image(self, state: str):
        """Fond d'une vignette (carte arrondie avec ombre douce) : normal, hover, current."""
        def build():
            c = COLORS
            dark = self.settings.theme == "dark"
            base = state.replace("sel_", "")
            border = {"normal": c["card_border"], "hover": c["card_hover_border"], "current": c["accent"]}[base]
            selected = state.startswith("sel_")
            fill = c["accent_soft"] if selected else c["card"]
            if selected:
                border = c["accent"]
            shadow = None if dark else (c["shadow"], 26 if base == "normal" else 46, 3 if base == "normal" else 5)
            return rounded_image(TILE_W, TILE_H, 10, fill, border, 2 if (base == "current" or selected) else 1,
                                 bg=c["bg"], shadow=shadow, inset=5)
        return self._custom(f"card:{state}", build)

    def well_image(self):
        return self._custom("well", lambda: rounded_image(THUMB_SIZE, THUMB_SIZE, 7, COLORS["well"], None,
                                                          bg=COLORS["card"]))

    def pill_image(self, w: int, h: int = 20, color: str = "#10141B", alpha: int = 170):
        w = max(h, int(w))
        return self._custom(f"pill:{w}:{h}:{color}:{alpha}",
                            lambda: rounded_image(w, h, h / 2, color, None, fill_alpha=alpha))

    def badge_image(self, icon: str, role: str):
        """Pastille ronde blanche avec icône (favori sur les vignettes)."""
        def build():
            img = rounded_image(26, 26, 13, "#FFFFFF", None, fill_alpha=235,
                                shadow=("#000000", 60, 1.5), inset=2)
            ic = draw_icon(icon, 14, COLORS[role])
            img.alpha_composite(ic, (6, 6))
            return img
        return self._custom(f"badge:{icon}:{role}", build)

    def badge_check(self):
        """Pastille bleue cochée (photo sélectionnée)."""
        def build():
            img = rounded_image(26, 26, 13, COLORS["accent_fill"], "#FFFFFF", 2, shadow=("#000000", 60, 1.5), inset=2)
            img.alpha_composite(draw_icon("check", 14, "#FFFFFF"), (6, 6))
            return img
        return self._custom("badge:check", build)

    def overlay_button(self, icon: str):
        def build():
            img = rounded_image(44, 44, 22, "#0B0E13", None, fill_alpha=120)
            img.alpha_composite(draw_icon(icon, 22, "#FFFFFF"), (11, 11))
            return img
        return self._custom(f"overlay:{icon}", build)

    _round_masks: dict = {}

    @classmethod
    def _round_mask(cls, size: tuple[int, int], r: int = 6):
        mask = cls._round_masks.get(size)
        if mask is None:
            ss = 3
            big = Image.new("L", (size[0] * ss, size[1] * ss), 0)
            ImageDraw.Draw(big).rounded_rectangle([0, 0, size[0] * ss - 1, size[1] * ss - 1], r * ss, fill=255)
            mask = big.resize(size, Image.LANCZOS)
            if len(cls._round_masks) > 400:
                cls._round_masks.clear()
            cls._round_masks[size] = mask
        return mask

    def rounded_thumbnail(self, img):
        """Coins arrondis anticrénelés pour une vignette (copie, l'image chargée n'est pas modifiée)."""
        try:
            out = img.convert("RGBA")
            out.putalpha(self._round_mask(out.size))
            return out
        except Exception:
            return img

    # -- thème clair / sombre -------------------------------------------------------------
    def set_theme(self, name: str) -> None:
        if name not in PALETTES:
            return
        old = dict(COLORS)
        self.settings.theme = name
        self.settings.save()
        if self.theme_var.get() != (name == "dark"):
            self.theme_var.set(name == "dark")
        COLORS.clear()
        COLORS.update(PALETTES[name])
        remap = {old[k]: COLORS[k] for k in old}
        self.theme.apply()
        self.icons.refresh()
        for img, build in self._custom_images.values():
            img.paste(build())
        self._apply_tk_colors(self.root)
        self.btn_theme.configure(image=self.icons.get("sun" if name == "dark" else "moon", 18, "secondary"))
        self._last_grid_msg = (self._last_grid_msg[0], remap.get(self._last_grid_msg[1], self._last_grid_msg[1]))
        for st in self._mode_states.values():
            if "grid_msg" in st:
                st["grid_msg"] = (st["grid_msg"][0], remap.get(st["grid_msg"][1], st["grid_msg"][1]))
        self._configure_tree_tags()
        if self.page_photos:
            self._layout()
        else:
            self._show_grid_message(*self._last_grid_msg)
        if self.viewer is not None and self.viewer.win.winfo_exists():
            self.viewer.apply_theme()
        self._set_status("Thème sombre activé." if name == "dark" else "Thème clair activé.")

    def toggle_theme(self) -> None:
        self.set_theme("light" if self.settings.theme == "dark" else "dark")

    def _apply_tk_colors(self, widget) -> None:
        """Recolore les widgets Tk classiques (les widgets ttk suivent leur style)."""
        for child in widget.winfo_children():
            self._apply_tk_colors(child)
        w = widget
        try:
            if isinstance(w, tk.Canvas):
                w.configure(bg=COLORS["viewer_bg"] if getattr(w, "_viewer", False) else COLORS["bg"])
            elif isinstance(w, tk.PanedWindow):
                w.configure(bg=COLORS["bg"])
            elif isinstance(w, tk.Menu):
                w.configure(bg=COLORS["panel"], fg=COLORS["text"], activebackground=COLORS["accent_soft"],
                            activeforeground=COLORS["accent_soft_text"], disabledforeground=COLORS["faint"],
                            selectcolor=COLORS["accent"])
            elif isinstance(w, tk.Tk):
                w.configure(bg=COLORS["bg"])
            elif isinstance(w, tk.Toplevel):
                w.configure(bg=COLORS["panel"])
            elif isinstance(w, tk.Frame):
                w.configure(bg=COLORS["separator"])
        except tk.TclError:
            pass

    def _configure_tree_tags(self) -> None:
        for t in (self.tree, self.ftree, getattr(self, "ctree", self.tree)):
            t.tag_configure("section", font=self.font_bold, foreground=COLORS["text"])
            t.tag_configure("empty", foreground=COLORS["faint"])
            t.tag_configure("folder", foreground=COLORS["text"])
            t.tag_configure("error", foreground=COLORS["missing"])
            t.tag_configure("hover", background=COLORS["row_hover"])

    def _tree_hover(self, tree, event) -> None:
        iid = tree.identify_row(event.y)
        old = getattr(tree, "_hover_iid", "")
        if iid == old:
            return
        if old and tree.exists(old):
            tree.item(old, tags=tuple(t for t in tree.item(old, "tags") if t != "hover"))
        tree._hover_iid = iid
        if iid and iid not in tree.selection():
            tree.item(iid, tags=tuple(tree.item(iid, "tags")) + ("hover",))

    def _tree_unhover(self, tree) -> None:
        old = getattr(tree, "_hover_iid", "")
        if old and tree.exists(old):
            tree.item(old, tags=tuple(t for t in tree.item(old, "tags") if t != "hover"))
        tree._hover_iid = ""

    # -- construction de l'interface ---------------------------------------
    def _build_ui(self) -> None:
        root = self.root
        ic = self.icons
        menubar = tk.Menu(root)
        m_file = tk.Menu(menubar, tearoff=False)
        m_file.add_command(label="Ouvrir une bibliothèque…", command=self.open_library_dialog,
                           accelerator="Ctrl+O")
        self.m_recent = tk.Menu(m_file, tearoff=False)
        m_file.add_cascade(label="Bibliothèques récentes", menu=self.m_recent)
        self._rebuild_recent_menu()
        m_file.add_command(label="Ouvrir un dossier…", command=self.open_folder_dialog,
                           accelerator="Ctrl+Maj+O")
        self.m_recent_folders = tk.Menu(m_file, tearoff=False)
        m_file.add_cascade(label="Dossiers récents", menu=self.m_recent_folders)
        self.m_fav_folders = tk.Menu(m_file, tearoff=False)
        m_file.add_cascade(label="Dossiers favoris", menu=self.m_fav_folders)
        self._rebuild_folder_menus()
        m_file.add_separator()
        m_file.add_command(label="Copier l'élément sélectionné vers un dossier…",
                           command=self.copy_selected_tree_item, accelerator="Ctrl+Maj+S")
        m_file.add_separator()
        m_file.add_command(label="Recharger", command=self.reload_current, accelerator="F5")
        m_file.add_command(label="Reconstruire l'index de la bibliothèque", command=self.rebuild_index)
        m_file.add_separator()
        m_file.add_command(label="Vider le cache des miniatures", command=self.clear_thumb_cache)
        m_file.add_command(label="Informations sur le cache", command=self.show_cache_info)
        m_file.add_command(label="Nettoyer les caches obsolètes…", command=self.clean_obsolete_caches)
        m_file.add_separator()
        m_file.add_command(label="Quitter", command=self.quit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="Fichier", menu=m_file)
        m_edit = tk.Menu(menubar, tearoff=False)
        m_edit.add_command(label="Sélectionner la page", command=self.select_page, accelerator="Ctrl+A")
        m_edit.add_command(label="Sélectionner toute la vue", command=self.select_view)
        m_edit.add_command(label="Inverser la sélection de la page", command=self.invert_page_selection)
        m_edit.add_command(label="Tout désélectionner", command=self.clear_selection, accelerator="Échap")
        m_edit.add_separator()
        m_edit.add_command(label="Ajouter la sélection à un album virtuel…", command=self.add_to_virtual_album)
        m_edit.add_command(label="Propriétés de la sélection", command=self.batch_properties)
        menubar.add_cascade(label="Édition", menu=m_edit)
        m_view = tk.Menu(menubar, tearoff=False)
        self.theme_var = tk.BooleanVar(value=self.settings.theme == "dark")
        m_view.add_checkbutton(label="Thème sombre", variable=self.theme_var,
                               command=lambda: self.set_theme("dark" if self.theme_var.get() else "light"))
        m_view.add_separator()
        m_view.add_command(label="Chronologie", command=lambda: self._select_tree_entry("timeline"))
        self.watch_var = tk.BooleanVar(value=self.settings.folder_watch)
        m_view.add_checkbutton(label="Surveiller le dossier ouvert (toutes les 30 s)", variable=self.watch_var,
                               command=self.toggle_folder_watch)
        menubar.add_cascade(label="Affichage", menu=m_view)
        m_tools = tk.Menu(menubar, tearoff=False)
        m_tools.add_command(label="Recherche avancée…", command=self.open_advanced_search, accelerator="Ctrl+Maj+F")
        m_tools.add_command(label="Carte des photos géolocalisées", command=self.open_map)
        m_tools.add_command(label="Statistiques", command=self.open_stats)
        m_tools.add_separator()
        m_tools.add_command(label="Rechercher les doublons…", command=self.find_duplicates)
        m_tools.add_command(label="Derniers résultats de doublons", command=self.open_last_duplicates)
        m_tools.add_command(label="Comparer les deux photos sélectionnées", command=self.compare_selection)
        m_tools.add_separator()
        m_tools.add_command(label="Diaporama", command=self.start_slideshow)
        m_tools.add_command(label="Exporter les métadonnées (CSV, JSON)…", command=lambda: ExportDialog(self, self.root))
        m_tools.add_command(label="Nouvel album virtuel…", command=self.new_valbum)
        m_tools.add_separator()
        m_tools.add_command(label="Analyser les métadonnées (EXIF, GPS)…", command=self.analyze_metadata)
        m_tools.add_command(label="Importer mots-clés, personnes et lieux Apple…", command=self.import_apple_terms)
        menubar.add_cascade(label="Outils", menu=m_tools)
        m_help = tk.Menu(menubar, tearoff=False)
        m_help.add_command(label="Raccourcis clavier", command=self.show_shortcuts)
        m_help.add_command(label="Diagnostic", command=self.show_diagnostic)
        m_help.add_command(label="À propos", command=self.show_about)
        menubar.add_cascade(label="Aide", menu=m_help)
        root.config(menu=menubar)
        root.bind_all("<Control-q>", lambda e: self.quit())
        root.bind_all("<Control-o>", lambda e: self.open_library_dialog())
        root.bind_all("<Control-O>", lambda e: self.open_folder_dialog())
        root.bind_all("<F5>", lambda e: self.reload_current())
        root.bind("<Control-S>", lambda e: self.copy_selected_tree_item())
        # Raccourcis portant sur la vignette survolée (fenêtre principale uniquement)
        for seq, action in (("<Control-c>", "copy"), ("<Control-C>", "path"), ("<Control-s>", "to"),
                            ("<KeyPress-f>", "fav"), ("<KeyPress-t>", "tags"), ("<KeyPress-i>", "exif")):
            root.bind(seq, lambda e, a=action: self._grid_shortcut(a))
        root.bind_all("<Control-f>", lambda e: self.search_entry.focus_set())
        root.bind_all("<Control-F>", lambda e: self.open_advanced_search())
        root.bind("<Control-a>", lambda e: self.select_page())
        root.bind("<Escape>", self.clear_selection)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        # ---- Barre supérieure : identité, mode, ouverture, recherche --------------------
        top = ttk.Frame(root, style="Panel.TFrame", padding=(SPACE_L, SPACE_S + 2, SPACE_L, SPACE_S + 2))
        top.pack(side="top", fill="x")
        self.topbar = top
        top_left = ttk.Frame(top, style="Panel.TFrame")     # empaqueté après la partie droite (prioritaire)
        brand = ttk.Frame(top_left, style="Panel.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, image=ic.get("logo", 28), style="Panel.TLabel").pack(side="left")
        self.brand_label = ttk.Label(brand, text="iphotor", style="Brand.TLabel")
        self.brand_label.pack(side="left", padx=(SPACE_S, 0))

        self.mode_var = tk.StringVar(value=MODE_LIBRARY)
        seg = ttk.Frame(top_left, style="Segment.TFrame", padding=3)
        seg.pack(side="left", padx=(SPACE_XL, 0))
        self.mode_buttons = {}
        for mode, label, short, icon in ((MODE_LIBRARY, "Bibliothèque iPhoto", "Bibliothèque", "library"),
                                         (MODE_FOLDER, "Explorer un dossier", "Dossier", "folder_open"),
                                         (MODE_COLLECTIONS, "Collections", "Collections", "album")):
            b = ttk.Radiobutton(seg, text=label, value=mode, variable=self.mode_var, image=ic.get(icon, 16),
                                compound="left", style="Segment.Toolbutton",
                                command=lambda: self.set_mode(self.mode_var.get()))
            b.pack(side="left", padx=(0 if mode == MODE_LIBRARY else 2, 0))
            b._labels = (label, short)
            self.mode_buttons[mode] = b

        self.btn_open_lib = ttk.Button(top_left, text="Ouvrir une bibliothèque…", image=ic.get("album", 16),
                                       compound="left", style="Ghost.TButton", command=self.open_library_dialog)
        self.btn_open_lib.pack(side="left", padx=(SPACE_M, 0))
        Tooltip(self.btn_open_lib, "Ouvrir une bibliothèque iPhoto ou Photos (Ctrl+O)")
        self.btn_open_dir = ttk.Button(top_left, text="Ouvrir un dossier…", image=ic.get("folder_open", 16),
                                       compound="left", style="Ghost.TButton", command=self.open_folder_dialog)
        self.btn_open_dir.pack(side="left", padx=(2, 0))
        Tooltip(self.btn_open_dir, "Explorer les images d'un dossier (Ctrl+Maj+O)")

        self.btn_theme = ttk.Button(top, image=ic.get("sun" if self.settings.theme == "dark" else "moon", 18),
                                    style="Icon.TButton", command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=(SPACE_S, 0))
        Tooltip(self.btn_theme, "Basculer entre thème clair et thème sombre")

        search = ttk.Frame(top, style="Panel.TFrame")
        search.pack(side="right")
        self.search_box = ttk.Frame(search, style="Search.TFrame", padding=(9, 2, 3, 2))
        self.search_box.pack(side="left")
        self.search_icon = ttk.Label(self.search_box, image=ic.get("search", 16, "muted"), style="SearchIcon.TLabel")
        self.search_icon.pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(self.search_box, textvariable=self.search_var, width=26, style="Search.TEntry")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda e: self.do_search())
        self.search_entry.bind("<Escape>", lambda e: self.clear_search())
        self.btn_clear = ttk.Button(self.search_box, image=ic.get("close", 14, "muted"), style="SearchClear.TButton",
                                    command=self.clear_search, takefocus=False)
        self.btn_clear.pack(side="left")
        Tooltip(self.btn_clear, "Effacer la recherche (Échap)")
        self.search_entry.bind("<FocusIn>", lambda e: self._search_focus(True), add="+")
        self.search_entry.bind("<FocusOut>", lambda e: self._search_focus(False), add="+")
        self.search_box.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.search_icon.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.scope_var = tk.StringVar(value="Toute la photothèque")
        self.scope = ttk.Combobox(search, textvariable=self.scope_var, state="readonly", width=19,
                                  values=("Toute la photothèque", "Album sélectionné"))
        self.scope.pack(side="left", padx=(SPACE_S, 0))
        self.scope.bind("<<ComboboxSelected>>", lambda e: self.scope.selection_clear())
        self.btn_search = ttk.Button(search, text="Rechercher", style="Accent.TButton", command=self.do_search,
                                     image=ic.get("search", 16, "on_accent"), compound="none")
        self.btn_search.pack(side="left", padx=(SPACE_S, 0))
        Tooltip(self.btn_search, "Rechercher dans les légendes, commentaires, noms de fichiers… (Ctrl+F)")
        top_left.pack(side="left", fill="x")
        ttk.Separator(root, orient="horizontal").pack(side="top", fill="x")

        # ---- Barre d'état ---------------------------------------------------------------
        status_bar = ttk.Frame(root, style="Footer.TFrame", padding=(SPACE_M, 0))
        status_bar.pack(side="bottom", fill="x")
        ttk.Separator(root, orient="horizontal").pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="")
        ro = ttk.Label(status_bar, text="Lecture seule", image=ic.get("lock", 12, "muted"), compound="left",
                       style="StatusIcon.TLabel", padding=(SPACE_S, 4))
        ro.pack(side="right")
        Tooltip(ro, "Les bibliothèques et les dossiers consultés ne sont jamais modifiés.")
        self.sel_label = ttk.Label(status_bar, text="", style="Status.TLabel", padding=(SPACE_M, 4))
        self.sel_label.pack(side="right")
        status = ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel", anchor="w",
                           padding=(0, 5))
        status.pack(side="left", fill="x", expand=True)

        # ---- Corps : panneau latéral + contenu ---------------------------------------------
        body = tk.PanedWindow(root, orient="horizontal", sashwidth=5, bg=COLORS["bg"], bd=0,
                              sashcursor="sb_h_double_arrow", opaqueresize=True)
        body.pack(side="top", fill="both", expand=True)
        self.body_pane = body

        left_pane = ttk.Frame(body, style="Panel.TFrame")
        sep = tk.Frame(left_pane, width=1, bg=COLORS["separator"])
        sep.pack(side="right", fill="y")
        self.sidebar_caption = ttk.Label(left_pane, text="PHOTOTHÈQUE", style="Section.TLabel",
                                         padding=(SPACE_L, SPACE_M, SPACE_L, SPACE_XS))
        self.sidebar_caption.pack(side="top", fill="x")
        tree_frame = ttk.Frame(left_pane, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.tree_frame = tree_frame
        self.tree = ttk.Treeview(tree_frame, columns=("n",), show="tree", selectmode="browse",
                                 style="Sidebar.Treeview")
        self.tree.column("#0", width=200, stretch=True)
        self.tree.column("n", width=56, anchor="e", stretch=False)
        tsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", lambda e: self._on_tree_menu(e, self.tree))

        self.ftree_frame = ttk.Frame(left_pane, style="Panel.TFrame")
        self.ftree = ttk.Treeview(self.ftree_frame, columns=("n",), show="tree", selectmode="browse",
                                  style="Sidebar.Treeview")
        self.ftree.column("#0", width=200, stretch=True)
        self.ftree.column("n", width=56, anchor="e", stretch=False)
        fsb = ttk.Scrollbar(self.ftree_frame, orient="vertical", command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=fsb.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.ftree.bind("<<TreeviewSelect>>", self._on_ftree_select)
        self.ftree.bind("<Button-3>", lambda e: self._on_tree_menu(e, self.ftree))
        self.ctree_frame = ttk.Frame(left_pane, style="Panel.TFrame")
        self.ctree = ttk.Treeview(self.ctree_frame, columns=("n",), show="tree", selectmode="browse",
                                  style="Sidebar.Treeview")
        self.ctree.column("#0", width=200, stretch=True)
        self.ctree.column("n", width=56, anchor="e", stretch=False)
        csb = ttk.Scrollbar(self.ctree_frame, orient="vertical", command=self.ctree.yview)
        self.ctree.configure(yscrollcommand=csb.set)
        self.ctree.pack(side="left", fill="both", expand=True)
        csb.pack(side="right", fill="y")
        self.ctree.bind("<<TreeviewSelect>>", self._on_ctree_select)
        self.ctree.bind("<Button-3>", lambda e: self.valbum_menu(self.ctree.identify_row(e.y), e.x_root, e.y_root))
        for t in (self.tree, self.ftree, self.ctree):
            t.bind("<Motion>", lambda e, tr=t: self._tree_hover(tr, e), add="+")
            t.bind("<Leave>", lambda e, tr=t: self._tree_unhover(tr), add="+")
            t.bind("<<TreeviewSelect>>", lambda e, tr=t: self._tree_unhover(tr), add="+")
        self._configure_tree_tags()
        body.add(left_pane, minsize=220, width=320)

        content = ttk.Frame(body, style="Bg.TFrame")
        # En-tête du contenu : titre de la vue, options du mode dossier, pagination
        header = ttk.Frame(content, style="Header.TFrame", padding=(SPACE_XL, SPACE_L, SPACE_L, SPACE_S))
        header.pack(side="top", fill="x")
        self.header = header
        nav = ttk.Frame(header, style="Header.TFrame")
        nav.pack(side="right", anchor="n")
        self.btn_prev = ttk.Button(nav, text="Précédent", compound="left",
                                   image=(ic.get("chev_left", 16, "text"), "disabled", ic.get("chev_left", 16, "faint")),
                                   style="TButton", command=self.prev_page)
        self.btn_prev.pack(side="left")
        Tooltip(self.btn_prev, "Page précédente (Page préc.)")
        self.page_label = ttk.Label(nav, text="", style="Header.TLabel", width=13, anchor="center")
        self.page_label.pack(side="left", padx=SPACE_XS)
        self.btn_next = ttk.Button(nav, text="Suivant", compound="right",
                                   image=(ic.get("chev_right", 16, "text"), "disabled", ic.get("chev_right", 16, "faint")),
                                   style="TButton", command=self.next_page)
        self.btn_next.pack(side="left")
        Tooltip(self.btn_next, "Page suivante (Page suiv.)")

        # Barre de filtres du mode dossier (sous l'en-tête)
        self.folder_opts = ttk.Frame(content, style="Header.TFrame", padding=(SPACE_XL, 0, SPACE_L, SPACE_S))
        opts_inner = ttk.Frame(self.folder_opts, style="Panel.TFrame", padding=(SPACE_M, 3, 4, 3))
        opts_inner.pack(side="left")
        self.recursive_var = tk.BooleanVar(value=self.settings.folder_recursive)
        ttk.Checkbutton(opts_inner, text="Inclure les sous-dossiers", variable=self.recursive_var,
                        command=self._on_recursive_toggle).pack(side="left")
        ttk.Label(opts_inner, text="Trier par", style="Muted.TLabel").pack(side="left", padx=(SPACE_L, SPACE_S))
        labels = [lbl for lbl, _k, _d in FOLDER_SORTS]
        self.sort_var = tk.StringVar(value=self.settings.folder_sort if self.settings.folder_sort in labels else labels[0])
        self.sort_box = ttk.Combobox(opts_inner, textvariable=self.sort_var, state="readonly",
                                     width=26, values=labels)
        self.sort_box.pack(side="left")
        self.sort_box.bind("<<ComboboxSelected>>", self._on_sort_change)
        key, desc = self._sort_choice()
        self.folder.sort_key, self.folder.sort_desc = key, desc

        self.btn_back = ttk.Button(header, text="Retour", compound="left", style="TButton",
                                   image=ic.get("chev_left", 16, "text"), command=self.timeline_back)
        Tooltip(self.btn_back, "Revenir au niveau précédent de la chronologie")
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)
        self.header_left = left
        self.title_label = ttk.Label(left, text="iphotor", style="Heading.TLabel")
        self.title_label.pack(anchor="w")
        self.subtitle_label = ttk.Label(left, text="", style="Subheading.TLabel")
        self.subtitle_label.pack(anchor="w", pady=(2, 0))

        # Barre d'actions contextuelle (sélection multiple)
        self.selbar = ttk.Frame(content, style="Header.TFrame", padding=(SPACE_XL, 0, SPACE_L, SPACE_S))
        sb_inner = ttk.Frame(self.selbar, style="Panel.TFrame", padding=(SPACE_M, 3, 4, 3))
        sb_inner.pack(fill="x")
        ttk.Label(sb_inner, image=ic.get("check", 16, "accent"), style="Panel.TLabel").pack(side="left")
        self.selbar_count = ttk.Label(sb_inner, text="", style="Strong.TLabel")
        self.selbar_count.pack(side="left", padx=(SPACE_XS, SPACE_M))

        self._selbar_buttons: list = []

        def sbtn(text, icon, cmd, tip, role="secondary"):
            b = ttk.Button(sb_inner, text=text, image=ic.get(icon, 16, role), compound="left" if text else "image",
                           style="Ghost.TButton" if text else "Icon.TButton", command=cmd)
            b.pack(side="left", padx=(2, 0))
            Tooltip(b, tip)
            if text:
                self._selbar_buttons.append(b)
            return b
        sbtn("Favoris", "heart_fill", lambda: self.batch_favorite(True), "Ajouter la sélection aux favoris", "favorite")
        sbtn("", "heart", lambda: self.batch_favorite(False), "Retirer la sélection des favoris")
        sbtn("Tag", "tag", self.batch_add_tags, "Ajouter un ou plusieurs tags")
        sbtn("", "close", self.batch_remove_tag, "Retirer un tag de la sélection")
        sbtn("Album virtuel", "album", self.add_to_virtual_album, "Ajouter à un album virtuel (références)")
        sbtn("Copier vers…", "folder_arrow", self.batch_copy, "Copier les fichiers vers un dossier")
        sbtn("Exporter", "file", lambda: ExportDialog(self, self.root, "selection"), "Exporter les métadonnées")
        sbtn("Infos", "info", self.batch_properties, "Propriétés communes de la sélection")
        self.btn_sel_compare = sbtn("Comparer", "library", self.compare_selection, "Comparer deux photos")
        sbtn("Diaporama", "play", lambda: self.start_slideshow("selection"), "Diaporama de la sélection")
        self.btn_sel_up = ttk.Button(sb_inner, image=ic.get("chev_left", 16), style="Icon.TButton",
                                     command=lambda: self.valbum_move_selection(-1))
        Tooltip(self.btn_sel_up, "Avancer dans l'album virtuel")
        self.btn_sel_down = ttk.Button(sb_inner, image=ic.get("chev_right", 16), style="Icon.TButton",
                                       command=lambda: self.valbum_move_selection(1))
        Tooltip(self.btn_sel_down, "Reculer dans l'album virtuel")
        self.btn_sel_remove = ttk.Button(sb_inner, text="Retirer de l'album", style="Ghost.TButton",
                                         command=self.valbum_remove_selection)
        clear = ttk.Button(sb_inner, image=ic.get("close", 14, "muted"), style="Icon.TButton",
                           command=self.clear_selection)
        clear.pack(side="right")
        Tooltip(clear, "Tout désélectionner (Échap)")

        grid_frame = ttk.Frame(content, style="Bg.TFrame")
        grid_frame.pack(side="top", fill="both", expand=True)
        self.grid_frame = grid_frame
        self.canvas = tk.Canvas(grid_frame, bg=COLORS["bg"], highlightthickness=0)
        gsb = ttk.Scrollbar(grid_frame, orient="vertical", command=self.canvas.yview,
                            style="Grid.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=gsb.set)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(SPACE_S, 0))
        gsb.pack(side="right", fill="y")
        body.add(content, minsize=420)

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Motion>", self._on_canvas_motion)
        self.canvas.bind("<Button-3>", self._on_canvas_menu)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        for widget in (self.canvas,):
            widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
            widget.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.configure(yscrollincrement=20)
        root.bind("<Prior>", lambda e: self.prev_page() if not self._focus_in_entry() else None)
        root.bind("<Next>", lambda e: self.next_page() if not self._focus_in_entry() else None)
        root.bind("<Configure>", self._on_root_resize, add="+")

        # Indicateur de chargement (au centre de la grille)
        self.progress = ttk.Progressbar(grid_frame, mode="indeterminate", length=260,
                                        style="Grid.Horizontal.TProgressbar")
        self._update_nav()

    def _search_focus(self, focused: bool) -> None:
        self.search_box.configure(style="SearchFocus.TFrame" if focused else "Search.TFrame")
        self.search_icon.configure(style="SearchIconFocus.TLabel" if focused else "SearchIcon.TLabel")
        self.btn_clear.configure(style="SearchClearFocus.TButton" if focused else "SearchClear.TButton")

    def _on_root_resize(self, event) -> None:
        """Adapte la barre supérieure à la largeur de la fenêtre (libellés réduits, puis icônes seules)."""
        if event.widget is not self.root:
            return
        w = event.width
        level = 0 if w >= 1480 else (1 if w >= 1180 else 2)
        if level == getattr(self, "_compact_level", None):
            return
        self._compact_level = level
        for b in self.mode_buttons.values():
            b.configure(text=b._labels[0] if level == 0 else b._labels[1])
        for b in (self.btn_open_lib, self.btn_open_dir):
            b.configure(compound="left" if level == 0 else "image",
                        style="Ghost.TButton" if level == 0 else "Icon.TButton")
        self.brand_label.pack_forget() if level == 2 else self.brand_label.pack(side="left", padx=(SPACE_S, 0))
        self.search_entry.configure(width={0: 28, 1: 22, 2: 16}[level]
                                    + (6 if self.mode == MODE_FOLDER else 0))
        self.scope.configure(width=19 if level < 2 else 14)
        self.btn_search.configure(compound="none" if level < 2 else "image",
                                  style="Accent.TButton" if level < 2 else "AccentIcon.TButton")
        for b in getattr(self, "_selbar_buttons", []):
            b.configure(compound="left" if level == 0 else "image",
                        style="Ghost.TButton" if level == 0 else "Icon.TButton")
        for b, txt in ((self.btn_prev, "Précédent"), (self.btn_next, "Suivant")):
            b.configure(text=txt if level < 2 else "", compound=("left" if b is self.btn_prev else "right")
                        if level < 2 else "image")

    def _focus_in_entry(self) -> bool:
        return self.root.focus_get() is self.search_entry

    # -- choix de la bibliothèque -----------------------------------------------
    def _update_title(self) -> None:
        name = os.path.basename(self.library.root) or self.library.root
        if self.mode == MODE_COLLECTIONS:
            name = "Collections"
        if self.mode == MODE_FOLDER and not name:
            name = "Explorer un dossier"
        name = safe_text(name)
        self.root.title(f"{name} — iphotor {APP_VERSION}")
        if hasattr(self, "sidebar_caption"):
            caption = {MODE_FOLDER: "DOSSIER", MODE_COLLECTIONS: "COLLECTIONS"}.get(self.mode, "PHOTOTHÈQUE")
            if name and name not in ("Explorer un dossier", "Collections"):
                caption += "  ·  " + re.sub(r"\.photo(s)?library$", "", name).upper()
            self.sidebar_caption.config(text=caption)

    def _rebuild_recent_menu(self) -> None:
        self.m_recent.delete(0, "end")
        recent = self.settings.recent or [self.library.root]
        for path in recent:
            label = os.path.basename(path) or path
            if path == self.library.root:
                label = "✓ " + label
            self.m_recent.add_command(label=f"{label}   —   {os.path.dirname(path)}",
                                      command=lambda p=path: self.switch_library(p))

    def open_library_dialog(self) -> None:
        if self._building:
            messagebox.showinfo("Indexation en cours",
                                "Attendez la fin de l'indexation avant de changer de bibliothèque.",
                                parent=self.root)
            return
        LibraryDialog(self)

    def switch_library(self, path: str) -> None:
        """Ouvre une autre bibliothèque (son index et ses miniatures ont leur propre cache)."""
        if self._building:
            messagebox.showinfo("Indexation en cours",
                                "Attendez la fin de l'indexation avant de changer de bibliothèque.",
                                parent=self.root)
            return
        path = os.path.abspath(path.rstrip("/"))
        library = LibraryManager(path)
        if not library.is_available():
            messagebox.showerror("Bibliothèque inaccessible", library.unavailable_message(), parent=self.root)
            return
        try:
            cache = CacheManager(library)
            cache.ensure_dirs()
        except (PermissionError, OSError) as exc:
            messagebox.showerror("Cache inaccessible", f"Impossible de préparer le cache local :\n{exc}",
                                 parent=self.root)
            return
        if self.mode != MODE_LIBRARY:
            self.set_mode(MODE_LIBRARY, restore=False)
        if self.viewer is not None and self.viewer.win.winfo_exists():
            self.viewer.close()
        self.loader.shutdown()
        self.library = library
        self.cache = cache
        self.loader = ThumbnailLoader(library, cache)
        self.settings.remember(path)
        self.clear_selection()
        self._timeline = None
        self._disk_ok = None
        self._adv_views = {}
        self._rebuild_recent_menu()
        self._update_title()
        self.search_var.set("")
        self.current_album_id = None
        self.status_var.set("")
        self.start(False)

    # -- modes : bibliothèque iPhoto / explorer un dossier ---------------------------
    # Attributs propres à chaque mode, sauvegardés et restaurés lors d'un changement de mode.
    _MODE_ATTRS = ("library", "cache", "loader", "photos", "view", "total", "page_no",
                   "view_ids", "current_album_id", "_annot_sel", "_tag_names", "_tl_stack", "_adv_views")

    def _initial_collections_state(self) -> dict:
        lib = CollectionLibrary(self._protected_roots)
        cache = FolderThumbCache(lib)
        return {
            "library": lib, "cache": cache, "loader": ThumbnailLoader(lib, cache),
            "photos": None, "view": View("empty", title="Albums virtuels"), "total": 0,
            "page_no": 0, "view_ids": None, "current_album_id": None, "search": "",
            "_annot_sel": None, "_tag_names": [], "_tl_stack": [], "_adv_views": {},
            "title": "Collections", "subtitle": "Albums virtuels : des photos de plusieurs sources, sans copie",
            "status": "", "yview": 0.0,
            "grid_msg": ("Les albums virtuels regroupent des photos de plusieurs bibliothèques et dossiers,\n"
                         "sans copier ni déplacer les fichiers.\n\nSélectionnez des photos (Ctrl+clic) dans une "
                         "bibliothèque ou un dossier,\npuis « Album virtuel » dans la barre de sélection.",
                         COLORS["muted"]),
        }

    def _initial_folder_state(self) -> dict:
        lib = FolderLibrary("")
        cache = FolderThumbCache(lib)
        return {
            "library": lib, "cache": cache, "loader": ThumbnailLoader(lib, cache),
            "photos": None, "view": View("empty", title="Explorer un dossier"), "total": 0,
            "page_no": 0, "view_ids": None, "current_album_id": None, "search": "",
            "_annot_sel": None, "_tag_names": [], "_tl_stack": [], "_adv_views": {},
            "title": "Explorer un dossier", "subtitle": "Aucun dossier ouvert", "status": "",
            "yview": 0.0,
            "grid_msg": ("Cliquez sur « Ouvrir un dossier… » pour afficher les images d'un dossier\n"
                         "de l'ordinateur ou d'un disque externe.\n\n"
                         "Formats affichés : JPG, JPEG, PNG, GIF, BMP et TIFF.\n"
                         "Les fichiers consultés ne sont jamais modifiés.", COLORS["muted"]),
        }

    def set_mode(self, mode: str, restore: bool = True) -> None:
        """Passe d'un mode à l'autre en conservant l'état de navigation de chacun."""
        if mode == self.mode:
            self.mode_var.set(mode)
            return
        if self._building:
            messagebox.showinfo("Indexation en cours",
                                "Attendez la fin de l'indexation de la bibliothèque avant de changer de mode.",
                                parent=self.root)
            self.mode_var.set(self.mode)
            return
        self.clear_selection()
        state = {a: getattr(self, a) for a in self._MODE_ATTRS}
        state.update(search=self.search_var.get(), title=self.title_label.cget("text"),
                     subtitle=self.subtitle_label.cget("text"), status=self.status_var.get(),
                     yview=self.canvas.yview()[0], grid_msg=self._last_grid_msg)
        self._mode_states[self.mode] = state
        if self.viewer is not None and self.viewer.win.winfo_exists():
            self.viewer.close()
        self._clear_grid()
        self.progress.stop()
        self.progress.place_forget()

        self.mode = mode
        self.mode_var.set(mode)
        new = self._mode_states.get(mode) or (self._initial_collections_state() if mode == MODE_COLLECTIONS
                                              else self._initial_folder_state())
        for a in self._MODE_ATTRS:
            setattr(self, a, new[a])
        self.search_var.set(new["search"])
        self.title_label.config(text=new["title"])
        self.subtitle_label.config(text=new["subtitle"])
        self.status_var.set(new["status"])
        self._apply_mode_widgets()
        if not restore:
            return
        self._refresh_annotations_tree()
        self._update_nav()
        if self.photos is not None and self.view.kind != "empty" and self.total > 0:
            self.load_page()
            if new.get("yview"):
                self.canvas.yview_moveto(new["yview"])
        else:
            self._show_grid_message(*new["grid_msg"])
        if mode == MODE_FOLDER:
            if self._scanner is not None:
                self._show_scan_progress()
            elif (not self.folder.root and self.settings.last_folder
                  and os.path.isdir(self.settings.last_folder)):
                self.open_folder(self.settings.last_folder)

    def _apply_mode_widgets(self) -> None:
        level = getattr(self, "_compact_level", 0) or 0
        for fr in (self.tree_frame, self.ftree_frame, self.ctree_frame):
            fr.pack_forget()
        if self.btn_back.winfo_ismapped():
            self.btn_back.pack_forget()
        if self.mode == MODE_FOLDER:
            self.ftree_frame.pack(fill="both", expand=True)
            self.scope.pack_forget()
            self.folder_opts.pack(side="top", fill="x", before=self.grid_frame)
            self.search_entry.configure(width={0: 28, 1: 22, 2: 16}[level] + 6)
        elif self.mode == MODE_COLLECTIONS:
            self.ctree_frame.pack(fill="both", expand=True)
            self.scope.pack_forget()
            self.folder_opts.pack_forget()
            self.search_entry.configure(width={0: 28, 1: 22, 2: 16}[level] + 6)
            self._populate_ctree()
        else:
            self.tree_frame.pack(fill="both", expand=True)
            self.folder_opts.pack_forget()
            self.scope.pack(side="left", padx=(SPACE_S, 0), before=self.btn_search)
            self.search_entry.configure(width={0: 28, 1: 22, 2: 16}[level])
        self._update_title()

    def reload_current(self) -> None:
        if self.mode == MODE_FOLDER:
            if self.library.root:
                self.open_folder(self.library.root)
            else:
                self.open_folder_dialog()
        else:
            self.start(False)

    def rebuild_index(self) -> None:
        if self.mode != MODE_LIBRARY:
            self.set_mode(MODE_LIBRARY, restore=False)
            if self.mode != MODE_LIBRARY:
                return
        self.start(True)

    # -- explorer un dossier ----------------------------------------------------------
    def open_folder_dialog(self) -> None:
        if self._building:
            messagebox.showinfo("Indexation en cours",
                                "Attendez la fin de l'indexation de la bibliothèque.", parent=self.root)
            return
        initial = (self.library.root if self.mode == MODE_FOLDER and self.library.root else
                   self.settings.last_folder) or os.path.expanduser("~")
        if not os.path.isdir(initial):
            initial = os.path.expanduser("~")
        path = filedialog.askdirectory(parent=self.root, title="Ouvrir un dossier d'images",
                                       initialdir=initial, mustexist=True)
        if not path:
            return
        if self.mode != MODE_FOLDER:
            self.set_mode(MODE_FOLDER, restore=False)
            if self.mode != MODE_FOLDER:
                return
        self.open_folder(path)

    def open_folder(self, path: str) -> None:
        """Analyse un dossier (lecture seule) dans un thread, puis affiche ses images."""
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            messagebox.showerror("Dossier inaccessible",
                                 f"Ce dossier n'existe pas ou n'est plus accessible :\n{safe_text(path)}\n\n"
                                 "Vérifiez que le disque qui le contient est bien branché.", parent=self.root)
            return
        if not os.access(path, os.R_OK | os.X_OK):
            messagebox.showerror("Dossier inaccessible",
                                 f"Vous n'avez pas le droit de lire ce dossier :\n{safe_text(path)}",
                                 parent=self.root)
            return
        if self._scanner is not None:
            self._scanner.cancel()
        if self.viewer is not None and self.viewer.win.winfo_exists():
            self.viewer.close()
        lib = FolderLibrary(path)
        cache = FolderThumbCache(lib)
        try:
            cache.ensure_dirs()
        except (PermissionError, OSError):
            pass   # miniatures non mises en cache (le cache se trouverait dans le dossier consulté)
        self._clear_grid()
        self.loader.shutdown()
        self.library, self.cache = lib, cache
        self.loader = ThumbnailLoader(lib, cache)
        self.photos = None
        self.view = View("empty", title="Analyse du dossier")
        self.total = 0
        self.page_no = 0
        self.view_ids = None
        self.search_var.set("")
        self.ftree.delete(*self.ftree.get_children())
        self._update_nav()
        self._update_title()

        recursive = bool(self.recursive_var.get())
        self.settings.last_folder = path
        self.settings.folder_recursive = recursive
        self.settings.recent_folders = folderlib.remember(self.settings.recent_folders, path)
        self.settings.save()
        self._rebuild_folder_menus()
        self.clear_selection()

        scanner = FolderScanner(path, recursive)
        self._scanner = scanner
        self._show_scan_progress()

        def run():
            try:
                scanner.run(progress=lambda d, n: self._scan_queue.put(("progress", scanner, d, n)))
                self._scan_queue.put(("done", scanner, None, 0))
            except Exception as exc:
                traceback.print_exc()
                self._scan_queue.put(("error", scanner, exc, 0))

        threading.Thread(target=run, daemon=True, name="analyse-dossier").start()
        if not self._scan_polling:
            self._scan_polling = True
            self.root.after(100, self._poll_scan)

    def _show_scan_progress(self, text: str = "Recherche des images…") -> None:
        if self.mode != MODE_FOLDER or self._scanner is None:
            return
        sc = self._scanner
        self.title_label.config(text="Analyse du dossier")
        self.subtitle_label.config(text=safe_text(sc.root) + ("   ·   sous-dossiers inclus" if sc.recursive else ""))
        self._show_grid_message(text, COLORS["text"])
        self.progress.place(relx=0.5, rely=0.58, anchor="center")
        self.progress.start(12)

    def _poll_scan(self) -> None:
        try:
            while True:
                kind, scanner, a, b = self._scan_queue.get_nowait()
                if scanner is not self._scanner:
                    continue   # analyse annulée (autre dossier choisi entre-temps)
                if kind == "progress":
                    text = (f"Recherche des images…\n{plural(a, 'dossier parcouru', 'dossiers parcourus')}, "
                            f"{plural(b, 'image trouvée', 'images trouvées')}")
                    if self.mode == MODE_FOLDER:
                        self._show_grid_message(text, COLORS["text"])
                    elif MODE_FOLDER in self._mode_states:
                        self._mode_states[MODE_FOLDER]["grid_msg"] = (text, COLORS["text"])
                else:
                    self._scanner = None
                    self._finish_scan(scanner, a if kind == "error" else None)
        except queue.Empty:
            pass
        if self._scanner is not None:
            self.root.after(100, self._poll_scan)
        else:
            self._scan_polling = False

    def _finish_scan(self, scanner: FolderScanner, error: Optional[Exception]) -> None:
        active = self.mode == MODE_FOLDER
        if active:
            self.progress.stop()
            self.progress.place_forget()
        if error is not None:
            msg = f"L'analyse du dossier a échoué :\n{error}"
            if active:
                self._show_grid_message(msg, COLORS["missing"])
            else:
                self._mode_states[MODE_FOLDER]["grid_msg"] = (msg, COLORS["missing"])
            return
        self.folder.load(scanner)
        if active:
            self._annot_sel = None
        else:
            self._mode_states[MODE_FOLDER]["_annot_sel"] = None
        self._populate_ftree()
        summary = f"{plural(len(self.folder.photos), 'image')} dans {plural(scanner.dirs_seen, 'dossier')}"
        if scanner.errors:
            summary += f"   ·   ⚠ {plural(len(scanner.errors), 'dossier inaccessible', 'dossiers inaccessibles')}"
        if scanner.unreadable:
            summary += f"   ·   ⚠ {plural(scanner.unreadable, 'fichier illisible', 'fichiers illisibles')}"
        if not self.cache.usable():
            summary += "   ·   miniatures non mises en cache (le cache se trouve dans ce dossier)"
        changes = self._folder_snapshot(scanner)
        if changes:
            summary += f"   ·   {changes}"
        self._timeline = None
        if active:
            self.photos = self.folder
            self.set_view(self._folder_view())
            self._set_status(summary)
        else:
            view = self._folder_view()
            self._mode_states[MODE_FOLDER].update(
                photos=self.folder, view=view, total=self.folder.count(), page_no=0, view_ids=None,
                yview=0.0, title=view.title, subtitle=self._folder_subtitle(), status=summary)
        self._select_ftree("f:")

    def _select_ftree(self, iid: str) -> None:
        """Sélectionne une entrée de l'arborescence sans recharger la grille."""
        self._ftree_silent = True
        self.ftree.selection_set(iid)
        self.ftree.focus(iid)
        self.root.after_idle(lambda: setattr(self, "_ftree_silent", False))

    def _populate_ftree(self) -> None:
        t = self.ftree
        t.delete(*t.get_children())
        f = self.folder
        if not f.root:
            return
        totals: dict[str, int] = {"": 0}
        for rel, n in f.dir_counts.items():
            parts = rel.split("/") if rel else []
            for k in range(len(parts) + 1):
                key = "/".join(parts[:k])
                totals[key] = totals.get(key, 0) + n
        name = os.path.basename(f.root) or f.root
        ic = self.icons
        t.insert("", "end", iid="f:", text=" " + safe_text(name), values=(format_count(totals[""]),),
                 tags=("section",), open=True, image=ic.get("folder_open", 16, "accent"))
        for rel in sorted((k for k in totals if k), key=lambda k: (k.count("/"), natural_key(k))):
            parent = rel.rpartition("/")[0]
            t.insert("f:" + parent, "end", iid="f:" + rel, text=" " + rel.rpartition("/")[2],
                     values=(format_count(totals[rel]),), tags=("folder",), image=ic.get("folder", 16))
        if f.errors:
            t.insert("", "end", iid="err", text=" Dossiers inaccessibles", values=(format_count(len(f.errors)),),
                     tags=("error",), image=ic.get("warning", 16, "missing"))
            for i, (rel, _reason) in enumerate(f.errors):
                t.insert("err", "end", iid=f"err:{i}", text=safe_text(rel), tags=("error",))
        if self.mode == MODE_FOLDER:
            self._refresh_annotations_tree()

    def _on_ftree_select(self, _event=None) -> None:
        if (self.mode != MODE_FOLDER or not self.folder.root or getattr(self, "_ftree_silent", False)
                or self._tree_silent):
            return
        sel = self.ftree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == "err":
            self._set_status(f"⚠ {plural(len(self.folder.errors), 'dossier n’a', 'dossiers n’ont')} pas pu être "
                             "lus (droits insuffisants ou erreur de lecture) : ils sont ignorés.")
            return
        if iid.startswith("err:"):
            rel, reason = self.folder.errors[int(iid[4:])]
            self._set_status(f"⚠ Dossier inaccessible : {safe_text(rel)} — {reason}")
            return
        if self._v2_tree_select(self.ftree, iid):
            return
        if iid == "fav" or iid.startswith("tag:"):
            self.folder.date_range = None
            self._show_annotation_view("fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])]))
            self._update_back()
            return
        if iid == "sec:tags":
            self.ftree.item(iid, open=True)
            return
        self._annot_sel = None
        self.folder.date_range = None
        self.folder.only = None
        self.folder.subdir = iid[2:] or None
        self.folder.refresh()
        self.photos = self.folder
        self.set_view(self._folder_view())
        self._set_status(safe_text(os.path.join(self.folder.root, self.folder.subdir or "")))

    def _folder_view(self) -> View:
        f = self.folder
        name = f.subdir.rpartition("/")[2] if f.subdir else (os.path.basename(f.root) or f.root)
        if f.only is not None:
            title = f"Recherche « {f.query} » dans {f.only_title}" if f.query else f.only_title
            return View("folder", query=f.query, title=safe_text(title))
        if f.date_range is not None:
            return View("folder", query=f.query, title=getattr(f, "range_title", "Chronologie"))
        title = f"Recherche « {f.query} » dans {name}" if f.query else f"Dossier : {name}"
        return View("folder", query=f.query, title=safe_text(title))

    def _folder_subtitle(self) -> str:
        f = self.folder
        path = os.path.join(f.root, f.subdir) if f.subdir else f.root
        sub = f"{plural(f.count(), 'image')}   ·   {safe_text(path)}"
        if f.recursive:
            sub += "   ·   sous-dossiers inclus"
        return sub

    def _folder_search(self) -> None:
        if not self.folder.root or self.photos is None:
            return
        q = self.search_var.get().strip()
        self.folder.query = q
        self.folder.refresh()
        self.set_view(self._folder_view())
        if q:
            self._set_status(f"Recherche « {q} » : {plural(self.folder.count(), 'image')} "
                             "dont le nom contient ce texte.")

    def _sort_choice(self) -> tuple[str, bool]:
        for label, key, desc in FOLDER_SORTS:
            if label == self.sort_var.get():
                return key, desc
        return "name", False

    def _on_sort_change(self, _event=None) -> None:
        self.sort_box.selection_clear()
        self.settings.folder_sort = self.sort_var.get()
        self.settings.save()
        key, desc = self._sort_choice()
        self.folder.set_sort(key, desc)
        if self.mode == MODE_FOLDER and self.photos is not None:
            self.set_view(self._folder_view())

    def _on_recursive_toggle(self) -> None:
        self.settings.folder_recursive = bool(self.recursive_var.get())
        self.settings.save()
        if self.mode == MODE_FOLDER and self.library.root:
            self.open_folder(self.library.root)

    # -- démarrage / indexation ----------------------------------------------
    def start(self, force_reindex: bool) -> None:
        if self._building:
            return
        self._close_db()
        self._clear_grid()
        self.tree.delete(*self.tree.get_children())

        available = self.library.is_available()
        if available and (force_reindex or not self.cache.is_valid()):
            self._start_build()
            return
        if not available:
            if self.cache.index_exists():
                self._open_db()
                self._set_status("⚠ Bibliothèque inaccessible : affichage de l'index en cache "
                                 "(les vignettes déjà en cache restent visibles, les photos ne peuvent pas être ouvertes).")
                messagebox.showwarning("Bibliothèque inaccessible",
                                       self.library.unavailable_message() +
                                       "\n\nL'index enregistré précédemment est utilisé pour parcourir les albums.",
                                       parent=self.root)
            else:
                self._show_grid_message(self.library.unavailable_message(), COLORS["missing"])
                self.title_label.config(text="Bibliothèque inaccessible")
                self.subtitle_label.config(text=self.library.root)
                self._set_status("Bibliothèque inaccessible et aucun index en cache : "
                                 "choisissez une bibliothèque (Fichier › Ouvrir une bibliothèque…) "
                                 "ou explorez un dossier.")
                self.root.after(400, self.open_library_dialog)
            return
        self._open_db()

    def _start_build(self) -> None:
        self._building = True
        self.title_label.config(text="Indexation de la bibliothèque")
        self.subtitle_label.config(text="Première lecture ou bibliothèque modifiée : construction de l'index local…")
        self._show_grid_message("Indexation en cours…\nLecture de AlbumData.xml", COLORS["text"])
        self.progress.place(relx=0.5, rely=0.58, anchor="center")
        self.progress.start(12)

        def run():
            try:
                stats = self.cache.build(progress=lambda msg: self._build_queue.put(("progress", msg)))
                self._build_queue.put(("done", stats))
            except Exception as exc:
                traceback.print_exc()
                self._build_queue.put(("error", exc))

        threading.Thread(target=run, daemon=True, name="indexation").start()
        self.root.after(100, self._poll_build)

    def _poll_build(self) -> None:
        try:
            while True:
                kind, payload = self._build_queue.get_nowait()
                if kind == "progress":
                    self._set_status(payload)
                    self._show_grid_message("Indexation en cours…\n" + payload, COLORS["text"])
                elif kind == "done":
                    self._building = False
                    self.progress.stop()
                    self.progress.place_forget()
                    self._open_db()
                    self._set_status(
                        f"Index construit en {payload['seconds']} s : {plural(payload['photos'], 'photo')}, "
                        f"{plural(payload['albums'], 'album')}, {plural(payload['rolls'], 'événement')}.")
                    return
                elif kind == "error":
                    self._building = False
                    self.progress.stop()
                    self.progress.place_forget()
                    msg = str(payload)
                    if isinstance(payload, plistlib.InvalidFileException) or "plist" in msg.lower():
                        msg = "Le fichier AlbumData.xml est illisible ou endommagé.\n\n" + msg
                    self._show_grid_message("L'indexation a échoué.\n\n" + msg, COLORS["missing"])
                    messagebox.showerror("Erreur d'indexation", "Impossible de construire l'index :\n\n" + msg,
                                         parent=self.root)
                    if self.cache.index_exists():
                        self._open_db()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_build)

    def _open_db(self) -> None:
        if self.library.is_available() and self.settings.last_library != self.library.root:
            self.settings.remember(self.library.root)
            self._rebuild_recent_menu()
        try:
            self.con = self.cache.connect()
            meta = dict(self.con.execute("SELECT key, value FROM meta").fetchall())
            self.library.set_mac_root(meta.get("archive_path", ""))
            self.albums = AlbumManager(self.con)
            self.photos = PhotoManager(self.con)
        except sqlite3.Error as exc:
            self._close_db()
            self._show_grid_message(f"L'index local est endommagé ({exc}).\n"
                                    "Utilisez « Fichier › Reconstruire l'index ».", COLORS["missing"])
            return
        self._populate_tree()
        self._disk_ok = self.library.is_available()
        self._attach_meta()
        self.tree.selection_set("all")
        self.tree.focus("all")
        if not self.status_var.get():
            self._set_status(f"Index du {meta.get('built_at', '?').replace('T', ' à ')} — "
                             f"{plural(int(meta.get('photo_count', 0)), 'photo')}.")

    def _close_db(self) -> None:
        if self.con is not None:
            try:
                self.con.close()
            except sqlite3.Error:
                pass
        self.con = None
        self.albums = None
        self.photos = None

    # -- arborescence ---------------------------------------------------------
    def _populate_tree(self) -> None:
        t = self.tree
        t.delete(*t.get_children())
        am = self.albums
        total = self.con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        ic = self.icons
        t.insert("", "end", iid="all", text=" Toutes les photos", values=(format_count(total),), tags=("section",),
                 image=ic.get("photos", 16, "accent"))

        # Albums (liste à plat, avec le chemin des dossiers pour lever les homonymies)
        flat = am.albums_flat()
        t.insert("", "end", iid="sec:albums", text=" Albums", values=(format_count(len(flat)),), tags=("section",),
                 image=ic.get("album", 16, "accent"))
        homonyms: dict[tuple[str, str], int] = {}
        for a in flat:
            key = (a.name, am.path_label(a))
            homonyms[key] = homonyms.get(key, 0) + 1
        for a in flat:
            label = a.name
            path = am.path_label(a)
            if homonyms[(a.name, path)] > 1:
                label += self._homonym_hint(a)
            if path:
                label += f"   ({path})"
            t.insert("sec:albums", "end", iid=f"a:{a.id}:flat", text=" " + label,
                     image=ic.get("smart" if a.kind == KIND_SMART else "album", 16),
                     values=(format_count(a.photo_count),), tags=("empty",) if a.photo_count == 0 else ())

        # Événements, groupés par année
        groups = am.events_by_year()
        n_events = sum(len(e) for _, e in groups)
        t.insert("", "end", iid="sec:events", text=" Événements", values=(format_count(n_events),),
                 tags=("section",), image=ic.get("event", 16, "accent"))
        for year, events in groups:
            yid = f"y:{year if year is not None else 'none'}"
            ycount = sum(e.photo_count for e in events)
            t.insert("sec:events", "end", iid=yid,
                     text=" " + (str(year) if year is not None else "Date inconnue"),
                     values=(format_count(ycount),), image=ic.get("event", 16))
            for e in events:
                t.insert(yid, "end", iid=f"a:{e.id}:ev", text=" " + e.name,
                         values=(format_count(e.photo_count),), tags=("empty",) if e.photo_count == 0 else ())

        # Dossiers : hiérarchie complète
        t.insert("", "end", iid="sec:folders", text=" Dossiers", values=("",), tags=("section",),
                 image=ic.get("folder", 16, "accent"))

        def add_children(parent_iid: str, parent_id: Optional[int]) -> int:
            n = 0
            kids = am.children(parent_id)
            names: dict[str, int] = {}
            for child in kids:
                names[child.name] = names.get(child.name, 0) + 1
            for child in kids:
                name = child.name + (self._homonym_hint(child) if names[child.name] > 1 else "")
                if child.kind == KIND_FOLDER:
                    iid = f"a:{child.id}:tree"
                    t.insert(parent_iid, "end", iid=iid, text=" " + name, image=ic.get("folder", 16),
                             values=(format_count(child.photo_count),), tags=("folder",))
                    add_children(iid, child.id)
                    n += 1
                elif child.kind in (KIND_ALBUM, KIND_SMART, KIND_SPECIAL) and parent_id is not None:
                    t.insert(parent_iid, "end", iid=f"a:{child.id}:tree", text=" " + name,
                             image=ic.get("smart" if child.kind == KIND_SMART else "album", 16),
                             values=(format_count(child.photo_count),),
                             tags=("empty",) if child.photo_count == 0 else ())
            return n

        n_top = add_children("sec:folders", None)
        t.set("sec:folders", "n", format_count(n_top))
        t.item("sec:events", open=False)
        self._refresh_annotations_tree()

    @staticmethod
    def _homonym_hint(a: Album) -> str:
        """Complément affiché pour distinguer des albums portant le même nom."""
        if a.first_date is not None:
            return f"  · {format_date(a.first_date)}"
        return f"  · n° {a.id}"

    def _album_from_iid(self, iid: str) -> Optional[int]:
        parts = iid.split(":")
        if iid.startswith("a:") or iid.startswith("s:a:"):
            try:
                return int(parts[1] if parts[0] == "a" else parts[2])
            except (IndexError, ValueError):
                return None
        return None

    def _v2_tree_select(self, tree, iid: str) -> bool:
        """Entrées ajoutées par iphotor 2 (chronologie, résultats, albums intelligents, recherches)."""
        handled = True
        if iid == "timeline":
            self.show_timeline("years", push=False)
        elif iid == "adv":
            v = self._adv_views.get("adv")
            if isinstance(v, View):
                self.set_view(v)
            elif v:
                f = self.folder
                f.subdir, f.date_range = None, None
                f.only, f.only_title = set(v[1]), v[2]
                f.refresh()
                self.photos = f
                self.set_view(self._folder_view())
        elif iid.startswith("smart:"):
            self.open_smart(int(iid[6:]))
        elif iid.startswith("saved:"):
            self.open_saved_search(int(iid[6:]))
        elif iid in ("sec:smart", "sec:saved"):
            tree.item(iid, open=True)
        else:
            handled = False
        if handled or self._tl_stack:
            if not handled:
                self._tl_stack = []
            self._update_back()
        return handled

    def _select_tree_entry(self, iid: str) -> None:
        if self.mode == MODE_COLLECTIONS:
            self.set_mode(MODE_LIBRARY)
        t = self.ftree if self.mode == MODE_FOLDER else self.tree
        if t.exists(iid):
            t.selection_set(iid)
            t.see(iid)

    def _on_tree_click(self, event) -> None:
        """Un clic sur le libellé d'une rubrique (Albums, Événements…) la déplie ou la replie."""
        iid = self.tree.identify_row(event.y)
        if not iid.startswith("sec:"):
            return
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            return   # la flèche est déjà gérée par Tk
        self.tree.item(iid, open=not self.tree.item(iid, "open"))

    def _on_tree_select(self, _event=None) -> None:
        if self.photos is None or self._tree_silent or self.mode != MODE_LIBRARY:
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if self._v2_tree_select(self.tree, iid):
            return
        if iid == "fav" or iid.startswith("tag:"):
            self._show_annotation_view("fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])]))
            self._set_status(self.tree.item(iid, "text").strip())
            self._update_back()
            return
        self._annot_sel = None
        album_id = self._album_from_iid(iid)
        label = self.tree.item(iid, "text").strip()
        if label:
            self._set_status(label)
        if iid == "all":
            self.current_album_id = None
            self.set_view(View("all", title="Toutes les photos"))
        elif album_id is not None:
            a = self.albums.get(album_id)
            if a is None:
                return
            self.current_album_id = album_id
            kind_label = {KIND_FOLDER: "Dossier", KIND_EVENT: "Événement", KIND_SMART: "Album intelligent"}.get(a.kind, "Album")
            self.set_view(View("album", album_id=album_id, title=f"{kind_label} : {a.name}"))
        elif iid.startswith("y:"):
            ys = iid[2:]
            year = None if ys == "none" else int(ys)
            self.current_album_id = None
            self.set_view(View("year", year=year, title=f"Événements {ys if year else '(date inconnue)'}"))
        elif iid.startswith("sec:"):
            self.current_album_id = None
            hint = {
                "sec:tags": "Sélectionnez un tag pour afficher ses photos.",
                "sec:albums": "Sélectionnez un album dans la liste à gauche.",
                "sec:events": "Dépliez une année puis sélectionnez un événement.",
                "sec:folders": "Dépliez un dossier pour voir ses albums.",
            }.get(iid, "")
            self.set_view(View("empty", title=self.tree.item(iid, "text").strip()), message=hint)
        elif iid == "search":
            self.set_view(self._search_view())

    # -- vues et pagination ------------------------------------------------------
    def set_view(self, view: View, message: str = "") -> None:
        self.view = view
        self.view_ids = None
        self.page_no = 0
        try:
            if view.kind == "timeline":
                self.periods = self._timeline_periods()
                self.total = len(self.periods)
            else:
                self.total = self.photos.count(view) if (self.photos and view.kind != "empty") else 0
        except sqlite3.Error as exc:
            self.total = 0
            message = f"Erreur de lecture de l'index : {exc}"
        self.title_label.config(text=view.title or "iphotor")
        sub = plural(self.total, "photo") if view.kind != "empty" else ""
        if view.kind == "folder":
            sub = self._folder_subtitle()
        if view.kind == "ids" and view.query:
            sub += f"   ·   {view.query}"
        if view.kind == "valbum" and self.coll_browser is not None:
            miss = self.coll_browser.missing
            sub = plural(self.total, "photo") + (f"   ·   {miss} introuvable(s) — clic droit sur l'album › Retrouver"
                                                  if miss else "") + "   ·   références seulement, fichiers intacts"
        if view.kind == "timeline":
            t = self._timeline_data()
            n = sum(p.count for p in self.periods if p.level not in ("gap",))
            sub = plural(n, "photo")
            rich = t.richest_days(3)
            if rich and view.level == "years":
                sub += "   ·   jours les plus riches : " + ", ".join(f"{p.label} ({p.count})" for p in rich)
            if t.unknown and view.level == "years":
                sub += f"   ·   {t.unknown} sans date exploitable"
        if view.kind == "album" and self.albums:
            a = self.albums.get(view.album_id)
            if a:
                path = self.albums.path_label(a)
                if path:
                    sub += f"   ·   {path}"
                if a.kind == KIND_EVENT and a.date is not None:
                    sub += f"   ·   {format_date(a.date, with_time=False)}"
        self.subtitle_label.config(text=sub)
        self.load_page(message=message)
        if hasattr(self, "btn_back"):
            self._update_back()

    def page_count(self) -> int:
        return max(1, (self.total + PAGE_SIZE - 1) // PAGE_SIZE)

    def prev_page(self) -> None:
        if self.page_no > 0:
            self.page_no -= 1
            self.load_page()

    def next_page(self) -> None:
        if self.page_no < self.page_count() - 1:
            self.page_no += 1
            self.load_page()

    def _update_nav(self) -> None:
        pc = self.page_count()
        self.page_label.config(text=f"Page {self.page_no + 1} / {pc}" if self.total else "")
        self.btn_prev.state(["!disabled"] if self.page_no > 0 else ["disabled"])
        self.btn_next.state(["!disabled"] if self.page_no < pc - 1 else ["disabled"])

    def _clear_grid(self) -> None:
        self.generation += 1
        self.loader.cancel_pending()
        self.canvas.delete("all")
        self.tile_images.clear()    # libère les images de la page précédente
        self.page_photos = []
        self.hover_index = None

    def load_page(self, message: str = "") -> None:
        self._clear_grid()
        self._update_nav()
        if self.photos is None or self.view.kind == "empty" or self.total == 0:
            if not message:
                message = "Aucune photo." if self.view.kind != "empty" else ""
                if self.view.kind == "search":
                    message = f"Aucune photo ne correspond à « {self.view.query} »."
                elif self.view.kind == "album":
                    message = "Cet album ne contient aucune photo."
                elif self.view.kind == "valbum":
                    message = ("Cet album virtuel est vide.\nSélectionnez des photos dans une bibliothèque ou un "
                               "dossier, puis « Album virtuel » dans la barre de sélection.")
                elif self.view.kind == "timeline":
                    message = "Aucune photo datée."
                elif self.view.kind == "ids":
                    message = "Aucune photo ne correspond à ces critères."
                elif self.view.kind == "folder":
                    if self.folder.query:
                        message = f"Aucune image dont le nom contient « {self.folder.query} »."
                    else:
                        message = ("Aucune image (JPG, PNG, GIF, BMP, TIFF) dans ce dossier."
                                   + ("" if self.folder.recursive else
                                      "\nCochez « Inclure les sous-dossiers » pour chercher plus loin."))
            self._show_grid_message(message)
            return
        if self.view.kind == "timeline":
            self.page_photos = [self._timeline_photo(p.first_id) if p.count else None for p in self.periods]
            self.canvas.yview_moveto(0)
            self._layout()
            gen = self.generation
            for i, p in enumerate(self.page_photos):
                if p is not None:
                    self.loader.request(gen, i, p)
            return
        try:
            self.page_photos = self.photos.page(self.view, self.page_no * PAGE_SIZE, PAGE_SIZE)
        except sqlite3.Error as exc:
            self._show_grid_message(f"Erreur de lecture de l'index : {exc}", COLORS["missing"])
            return
        self.canvas.yview_moveto(0)
        self._layout()
        gen = self.generation
        for i, p in enumerate(self.page_photos):
            self.loader.request(gen, i, p)

    def _columns(self) -> int:
        return max(1, (self.canvas.winfo_width() - 10) // TILE_W)

    def _tile_origin(self, index: int) -> tuple[int, int]:
        cols = self._columns()
        width = self.canvas.winfo_width()
        margin = max(5, (width - cols * TILE_W) // 2)
        return margin + (index % cols) * TILE_W, 4 + (index // cols) * TILE_H

    def _layout(self) -> None:
        """Dessine (ou redessine) toutes les vignettes de la page."""
        self._layout_job = None
        self.canvas.delete("all")
        if not self.page_photos:
            return
        for i, p in enumerate(self.page_photos):
            if self.view.kind == "timeline":
                self._draw_period(i, self.periods[i])
            else:
                self._draw_tile(i, p)
        rows = (len(self.page_photos) + self._columns() - 1) // self._columns()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), rows * TILE_H + 16))

    def _truncate(self, text: str, font, width: int) -> str:
        if font.measure(text) <= width:
            return text
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + ell) <= width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ell

    def _draw_tile(self, i: int, p: Photo) -> None:
        c = self.canvas
        x, y = self._tile_origin(i)
        tag = f"t{i}"
        state = self.tile_images.get(i)
        look = self._tile_look(i)
        c.create_image(x, y, image=self.card_image(look), anchor="nw", tags=(tag, f"bg{i}"))
        # Zone de la miniature : carré neutre, photo centrée (proportions respectées)
        wx, wy = x + (TILE_W - THUMB_SIZE) // 2, y + 12
        cx, cy = wx + THUMB_SIZE // 2, wy + THUMB_SIZE // 2
        c.create_image(wx, wy, image=self.well_image(), anchor="nw", tags=(tag,))
        img_box = (wx, wy, wx + THUMB_SIZE, wy + THUMB_SIZE)
        if isinstance(state, dict) and state.get("img") is not None:
            c.create_image(cx, cy, image=state["img"], anchor="center", tags=(tag,))
            iw, ih = state.get("size", (THUMB_SIZE, THUMB_SIZE))
            img_box = (cx - iw // 2, cy - ih // 2, cx + iw // 2, cy + ih // 2)
        else:
            icon, label = "photos", ""
            if isinstance(state, dict):
                icon, label = {"video": ("video", "Vidéo"), "raw": ("image_off", "RAW sans aperçu"),
                               "missing": ("image_off", "Image introuvable"),
                               "error": ("warning", "Image illisible")}.get(state.get("status", ""),
                                                                            ("image_off", "Aucun aperçu"))
            c.create_image(cx, cy - (10 if label else 0), image=self.icons.get(icon, 32, "faint"), tags=(tag,))
            if label:
                c.create_text(cx, cy + 20, text=label, fill=COLORS["muted"], font=self.font_small,
                              justify="center", tags=(tag,))
        bx0, by0, bx1, by1 = img_box
        # Badges sur la photo : vidéo (haut gauche), note (haut droite), favori (bas droite)
        if p.is_video:
            w = self.font_badge.measure("VIDÉO") + 30
            c.create_image(bx0 + 6, by0 + 6, image=self.pill_image(w, 20), anchor="nw", tags=(tag,))
            c.create_image(bx0 + 15, by0 + 16, image=self.icons.get("play", 10, "white"), tags=(tag,))
            c.create_text(bx0 + 24, by0 + 16, text="VIDÉO", fill="#FFFFFF", font=self.font_badge, anchor="w",
                          tags=(tag,))
        if p.rating:
            stars = "★" * min(p.rating, 5)
            w = self.font_small.measure(stars) + 14
            c.create_image(bx1 - 6, by0 + 6, image=self.pill_image(w, 20), anchor="ne", tags=(tag,))
            c.create_text(bx1 - 6 - w // 2, by0 + 16, text=stars, fill=COLORS["star"], font=self.font_small,
                          tags=(tag,))
        if self.selection and self.is_selected(p):
            c.create_image(bx0 + 4, by1 - 4, image=self.badge_check(), anchor="sw", tags=(tag,))
        if self.is_favorite(p):
            c.create_image(bx1 - 4, by1 - 4, image=self.badge_image("heart_fill", "favorite"), anchor="se",
                           tags=(tag,))
        if isinstance(state, dict) and state.get("exists") is False:
            text = "Fichier introuvable"
            w = self.font_badge.measure(text) + 34
            c.create_image(cx, wy + THUMB_SIZE - 6, image=self.pill_image(w, 22, COLORS["missing"], 235),
                           anchor="s", tags=(tag,))
            c.create_image(cx - w // 2 + 14, wy + THUMB_SIZE - 17, image=self.icons.get("warning", 12, "white"),
                           tags=(tag,))
            c.create_text(cx - w // 2 + 24, wy + THUMB_SIZE - 17, text=text, fill="#FFFFFF", font=self.font_badge,
                          anchor="w", tags=(tag,))
        # Légende
        text_w = TILE_W - 28
        ty = wy + THUMB_SIZE + 10
        c.create_text(x + 14, ty, text=self._truncate(p.label, self.font, text_w), anchor="nw",
                      fill=COLORS["text"], font=self.font, tags=(tag,))
        sub = format_date(p.date, with_time=False) if p.date is not None else ""
        if p.label != p.filename and p.filename:
            sub = (sub + "  ·  " if sub else "") + p.filename
        if p.source == "folder" and p.size:
            sub = (sub + "  ·  " if sub else "") + format_size(p.size)
        c.create_text(x + 14, ty + 19, text=self._truncate(sub, self.font_small, text_w), anchor="nw",
                      fill=COLORS["muted"], font=self.font_small, tags=(tag,))

    def _redraw_tile(self, i: int) -> None:
        if 0 <= i < len(self.page_photos):
            self.canvas.delete(f"t{i}")
            if self.view.kind == "timeline":
                if i < len(self.periods):
                    self._draw_period(i, self.periods[i])
            else:
                self._draw_tile(i, self.page_photos[i])

    def _show_grid_message(self, text: str, color: Optional[str] = None) -> None:
        color = color or COLORS["muted"]
        self._last_grid_msg = (text, color)
        self.canvas.delete("all")
        if not text:
            return
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        icon = "warning" if color == COLORS["missing"] else ("photos" if color == COLORS["muted"] else "library")
        self.canvas.create_image(w // 2, h // 2 - 92, image=self.icons.get(icon, 48, "faint" if icon != "warning"
                                                                            else "missing"), tags=("msgicon",))
        self.canvas.create_text(w // 2, h // 2 - 52, text=text, fill=color, font=self.font_big, anchor="n",
                                justify="center", width=min(w - 80, 640), tags=("msg",))
        self.canvas.configure(scrollregion=(0, 0, w, h))

    def _on_canvas_resize(self, _event=None) -> None:
        if self._layout_job:
            self.root.after_cancel(self._layout_job)
        if self.page_photos:
            self._layout_job = self.root.after(80, self._layout)
        else:
            msg = self.canvas.find_withtag("msg")
            if msg:
                w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
                self.canvas.coords(msg[0], w // 2, h // 2 - 52)
                self.canvas.itemconfigure(msg[0], width=min(w - 80, 640))
                for item in self.canvas.find_withtag("msgicon"):
                    self.canvas.coords(item, w // 2, h // 2 - 92)

    def _index_at(self, event) -> Optional[int]:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        cols = self._columns()
        width = self.canvas.winfo_width()
        margin = max(5, (width - cols * TILE_W) // 2)
        col = int((x - margin) // TILE_W)
        row = int((y - 4) // TILE_H)
        if col < 0 or col >= cols or row < 0:
            return None
        i = row * cols + col
        return i if i < len(self.page_photos) else None

    def _set_hover(self, i: Optional[int]) -> None:
        if i == self.hover_index:
            return
        old = self.hover_index
        self.hover_index = i
        if old is not None:
            self.canvas.itemconfigure(f"bg{old}", image=self.card_image(self._tile_look(old)))
        if i is not None:
            self.canvas.itemconfigure(f"bg{i}", image=self.card_image(self._tile_look(i)))
            if self.view.kind == "timeline":
                per = self.periods[i] if i < len(self.periods) else None
                if per is not None:
                    hint = {"year": "clic : afficher les mois", "month": "clic : afficher les jours",
                            "day": "clic : afficher les photos", "unknown": "clic : afficher ces photos"}.get(per.level, "")
                    txt = per.label + ("" if per.level == "gap" else f"   ·   {plural(per.count, 'photo')}")
                    if hint and per.count:
                        txt += f"   ·   {hint}"
                    self.status_var.set(txt)
                self.canvas.configure(cursor="hand2" if per is not None and per.count else "")
                return
            p = self.page_photos[i]
            if p.source == "folder":
                parts = [safe_text(p.image_path), format_size(p.size)]
                if p.date is not None:
                    parts.append(format_date(p.date))
                state = self.tile_images.get(i)
                if isinstance(state, dict) and state.get("status") == "error":
                    parts.append("⚠ image illisible")
                parts += self._annotation_parts(p)
                self.status_var.set("   ·   ".join(parts))
                self.canvas.configure(cursor="hand2")
                return
            parts = [p.filename]
            if p.caption and p.caption != os.path.splitext(p.filename)[0]:
                parts.append(f"« {p.caption} »")
            if p.date is not None:
                parts.append(format_date(p.date))
            if p.event_name:
                parts.append(f"Événement : {p.event_name}")
            state = self.tile_images.get(i)
            if isinstance(state, dict) and state.get("exists") is False:
                parts.append("⚠ fichier introuvable")
            parts += self._annotation_parts(p)
            self.status_var.set("   ·   ".join(parts))
        self.canvas.configure(cursor="hand2" if i is not None else "")

    def _tile_look(self, i: int) -> str:
        if self.current_index == self.page_no * PAGE_SIZE + i:
            look = "current"
        else:
            look = "hover" if self.hover_index == i else "normal"
        if (self.selection and self.view.kind != "timeline" and 0 <= i < len(self.page_photos)
                and self.page_photos[i] is not None and self.is_selected(self.page_photos[i])):
            look = "sel_" + look
        return look

    def _annotation_parts(self, p: Photo) -> list[str]:
        parts = ["♥ favori"] if self.is_favorite(p) else []
        tags = self.tags_of(p)
        if tags:
            parts.append("Tags : " + ", ".join(tags))
        return parts

    def _on_canvas_motion(self, event) -> None:
        self._set_hover(self._index_at(event))

    def _on_canvas_click(self, event) -> None:
        i = self._index_at(event)
        if i is None:
            return
        if self.view.kind == "timeline":
            if i < len(self.periods):
                self.open_period(self.periods[i])
            return
        if event.state & 0x0004:          # Ctrl+clic : (dé)sélectionner
            self.toggle_select(i)
            return
        if event.state & 0x0001:          # Maj+clic : sélectionner une plage
            self.select_range(i)
            return
        self._sel_anchor = self.page_no * PAGE_SIZE + i
        self.open_viewer(self.page_no * PAGE_SIZE + i)

    def _poll_thumbnails(self) -> None:
        processed = 0
        try:
            while processed < 40:
                gen, index, _pid, img, exists, status = self.loader.results.get_nowait()
                processed += 1
                if gen != self.generation:
                    continue   # résultat d'une page qui n'est plus affichée
                tkimg = ImageTk.PhotoImage(self.rounded_thumbnail(img)) if img is not None else None
                self.tile_images[index] = {"img": tkimg, "exists": exists, "status": status,
                                           "size": img.size if img is not None else None}
                self._redraw_tile(index)
        except queue.Empty:
            pass
        self.root.after(30 if processed else 60, self._poll_thumbnails)

    # -- visionneuse -------------------------------------------------------------
    def open_viewer(self, absolute_index: int) -> None:
        if self.photos is None or self.view.kind == "timeline":
            return
        if self.view_ids is None:
            try:
                self.view_ids = self.photos.ids(self.view)
            except sqlite3.Error as exc:
                messagebox.showerror("Erreur", f"Lecture de l'index impossible : {exc}", parent=self.root)
                return
        if self.viewer is None or not self.viewer.win.winfo_exists():
            self.viewer = PhotoViewer(self)
        self.viewer.show(self.view_ids, absolute_index)

    def sync_page_to_index(self, absolute_index: int) -> None:
        """Garde la grille alignée sur la photo affichée dans la visionneuse."""
        page = absolute_index // PAGE_SIZE
        old = self.current_index
        self.current_index = absolute_index
        if page != self.page_no and 0 <= page < self.page_count():
            self.page_no = page
            self.load_page()
            return
        for idx in (old, absolute_index):     # met en évidence la photo affichée dans la visionneuse
            if idx is not None and idx // PAGE_SIZE == self.page_no:
                self._redraw_tile(idx - self.page_no * PAGE_SIZE)

    def viewer_closed(self) -> None:
        old, self.current_index = self.current_index, None
        if old is not None and old // PAGE_SIZE == self.page_no:
            self._redraw_tile(old - self.page_no * PAGE_SIZE)

    # -- recherche ---------------------------------------------------------------
    def _search_view(self) -> View:
        q = self.search_var.get().strip()
        if self.scope_var.get() == "Album sélectionné" and self.current_album_id is not None:
            a = self.albums.get(self.current_album_id)
            return View("search", album_id=self.current_album_id, query=q,
                        title=f"Recherche « {q} » dans {a.name if a else 'l’album'}")
        return View("search", query=q, title=f"Recherche « {q} » dans toute la photothèque")

    def do_search(self) -> None:
        if self.mode == MODE_FOLDER:
            self._folder_search()
            return
        if self.mode == MODE_COLLECTIONS:
            self._collection_search()
            return
        if self.photos is None:
            return
        q = self.search_var.get().strip()
        if not q:
            self.clear_search()
            return
        if self.scope_var.get() == "Album sélectionné" and self.current_album_id is None:
            messagebox.showinfo("Recherche",
                                "Aucun album n'est sélectionné : la recherche porte sur toute la photothèque.",
                                parent=self.root)
        view = self._search_view()
        t = self.tree
        if t.exists("search"):
            t.delete("search")
        n_photos = self.photos.count(view)
        t.insert("", 0, iid="search", text=f" Résultats : « {q} »", values=(format_count(n_photos),),
                 tags=("section",), open=True, image=self.icons.get("search", 16, "accent"))
        found = self.albums.search(q)
        for a in found:
            kind = {KIND_FOLDER: "folder", KIND_EVENT: "event", KIND_SMART: "smart"}.get(a.kind, "album")
            label = " " + a.name
            path = self.albums.path_label(a)
            if path:
                label += f"   ({path})"
            elif a.kind == KIND_EVENT and a.year:
                label += f"   ({a.year})"
            t.insert("search", "end", iid=f"s:a:{a.id}", text=label, values=(format_count(a.photo_count),),
                     image=self.icons.get(kind, 16))
        self._set_status(f"Recherche « {q} » : {plural(n_photos, 'photo')}, "
                         f"{plural(len(found), 'album ou événement', 'albums ou événements')} trouvés.")
        t.see("search")
        t.selection_set("search")   # déclenche set_view via _on_tree_select

    def clear_search(self) -> None:
        self.search_var.set("")
        if self.mode == MODE_FOLDER:
            self._folder_search()
            return
        if self.mode == MODE_COLLECTIONS:
            self._collection_search()
            return
        if self.tree.exists("search"):
            self.tree.delete("search")
            if self.tree.exists("all"):
                self.tree.selection_set("all")

    # -- favoris et tags -----------------------------------------------------------
    def _collection_search(self) -> None:
        b = self.coll_browser
        if b is None or self.view.kind != "valbum":
            return
        b.query = self.search_var.get().strip()
        b.refresh()
        self.set_view(self.view)

    def _scope(self) -> str:
        return FILES_SCOPE if self.mode in (MODE_FOLDER, MODE_COLLECTIONS) else library_scope(self.library.root)

    def _key(self, p: Photo) -> tuple[str, str]:
        if p.ref:
            return p.ref
        return photo_scope_key(p, self.library.root)

    def is_favorite(self, p: Photo) -> bool:
        try:
            return self.annotations.is_favorite(*self._key(p))
        except (sqlite3.Error, OSError):
            return False

    def tags_of(self, p: Photo) -> list[str]:
        try:
            return self.annotations.tags_of(*self._key(p))
        except (sqlite3.Error, OSError):
            return []

    def _annotation_error(self, exc: Exception, parent=None) -> None:
        messagebox.showerror("Favoris et tags",
                             f"Enregistrement impossible dans {ANNOTATIONS_PATH} :\n{exc}",
                             parent=parent or self.root)

    def toggle_favorite(self, p: Photo, parent=None) -> None:
        on = not self.is_favorite(p)
        try:
            self.annotations.set_favorite(*self._key(p), on)
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc, parent)
            return
        name = safe_text(p.filename or p.label)
        self._set_status(f"♥ {name} ajoutée aux favoris." if on else f"{name} retirée des favoris.")
        self._after_annotation_change()

    def edit_tags(self, p: Photo, parent=None) -> None:
        scope, key = self._key(p)
        try:
            current = self.annotations.tags_of(scope, key)
            known = self.annotations.all_tags(scope)
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc, parent)
            return
        dlg = TagDialog(self, parent or self.root, p, current, known)
        if dlg.result is None:
            return
        try:
            self.annotations.set_tags(scope, key, dlg.result)
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc, parent)
            return
        tags = self.annotations.tags_of(scope, key)
        self._set_status(f"Tags de {safe_text(p.filename or p.label)} : " + (", ".join(tags) if tags else "aucun"))
        self._after_annotation_change()

    def _annotation_keys(self, sel) -> set[str]:
        scope = self._scope()
        if sel == "fav":
            return self.annotations.favorites(scope)
        return self.annotations.keys_with_tag(scope, sel[1])

    @staticmethod
    def _annotation_title(sel) -> str:
        return "Mes favoris" if sel == "fav" else f"Tag : {sel[1]}"

    def _show_annotation_view(self, sel) -> None:
        """Affiche les favoris (sel = "fav") ou les photos d'un tag (sel = ("tag", nom))."""
        self._annot_sel = sel
        self.current_album_id = None
        try:
            keys = self._annotation_keys(sel)
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc)
            return
        empty = ("Aucune photo dans vos favoris.\nClic droit sur une photo › « Ajouter aux favoris »."
                 if sel == "fav" else "Aucune photo n'a ce tag.")
        if self.mode == MODE_FOLDER:
            f = self.folder
            f.subdir = None
            f.only = keys
            f.only_title = self._annotation_title(sel)
            f.refresh()
            self.photos = f
            self.set_view(self._folder_view(), message="" if f.count() else empty)
        else:
            view = View("keys", keys=tuple(sorted(keys)), title=self._annotation_title(sel))
            self.set_view(view, message=empty if not keys else "")

    def _annotation_count(self, keys: set[str]) -> int:
        if not keys:
            return 0
        if self.mode == MODE_FOLDER:
            return sum(1 for p in self.folder.photos if path_key(p.image_path) in keys)
        try:
            return self.photos.count(View("keys", keys=tuple(keys)))
        except sqlite3.Error:
            return 0

    def _refresh_annotations_tree(self) -> None:
        """(Re)crée les entrées « Mes favoris » et « Tags » de l'arborescence du mode courant."""
        if self.mode == MODE_FOLDER:
            t = self.ftree
            if not self.folder.root or not t.exists("f:"):
                return
            pos = 1
        else:
            t = self.tree
            if self.photos is None or not t.exists("all"):
                return
            pos = t.index("all") + 1
        scope = self._scope()
        try:
            favs = self.annotations.favorites(scope)
            tags = self.annotations.all_tags(scope)
            tag_keys = [self.annotations.keys_with_tag(scope, tg) for tg in tags]
        except (sqlite3.Error, OSError):
            return
        sel = t.selection()
        was_open = t.exists("sec:tags") and t.item("sec:tags", "open")
        for iid in ("timeline", "fav", "sec:tags"):
            if t.exists(iid):
                t.delete(iid)
        t.insert("", pos, iid="timeline", text=" Chronologie", values=("",), tags=("section",),
                 image=self.icons.get("event", 16, "accent"))
        pos += 1
        t.insert("", pos, iid="fav", text=" Mes favoris", values=(format_count(self._annotation_count(favs)),),
                 tags=("section",), image=self.icons.get("heart", 16, "favorite"))
        self._tag_names = tags
        if tags:
            t.insert("", pos + 1, iid="sec:tags", text=" Tags", values=(format_count(len(tags)),),
                     tags=("section",), open=bool(was_open), image=self.icons.get("tag", 16, "accent"))
            for i, (name, keys) in enumerate(zip(tags, tag_keys)):
                t.insert("sec:tags", "end", iid=f"tag:{i}", text=" " + name, image=self.icons.get("hash", 16),
                         values=(format_count(self._annotation_count(keys)),))
        # Resélectionne l'entrée courante sans recharger la grille.
        target = None
        if self._annot_sel == "fav":
            target = "fav"
        elif isinstance(self._annot_sel, tuple) and self._annot_sel[1] in tags:
            target = f"tag:{tags.index(self._annot_sel[1])}"
        elif sel and t.exists(sel[0]) and sel[0] != "sec:tags" and not sel[0].startswith("tag:"):
            target = sel[0]
        self.refresh_smart_entries()
        if target and t.exists(target):
            self._select_silently(t, target)

    def _select_silently(self, tree, iid: str) -> None:
        self._tree_silent = True
        tree.selection_set(iid)
        tree.focus(iid)
        tree.see(iid)
        self.root.after_idle(lambda: setattr(self, "_tree_silent", False))

    def _after_annotation_change(self) -> None:
        self._refresh_annotations_tree()
        if self._annot_sel is not None and self._annotation_view_active():
            page, y = self.page_no, self.canvas.yview()[0]
            self._show_annotation_view(self._annot_sel)
            page = min(page, self.page_count() - 1)
            if page > 0:
                self.page_no = page
                self.load_page()
            self.canvas.yview_moveto(y)
        elif self.page_photos:
            self._layout()        # met à jour les badges ♥ des vignettes
        if self.viewer is not None and self.viewer.win.winfo_exists():
            self.viewer.refresh_annotations()

    def _annotation_view_active(self) -> bool:
        if self.mode == MODE_FOLDER:
            return self.folder.only is not None and self.view.kind == "folder"
        return self.view.kind == "keys"

    # -- fichiers sources -------------------------------------------------------------
    def source_file(self, p: Photo, original: bool = False) -> Optional[str]:
        """Fichier à copier ou à analyser : version affichée ou original (lecture seule)."""
        return resolve_photo_file(self.library, p, original)

    # -- presse-papiers ----------------------------------------------------------------
    def copy_image(self, p: Photo, parent=None) -> None:
        parent = parent or self.root
        if p.is_video:
            messagebox.showinfo("Copier l'image", "Les vidéos ne peuvent pas être copiées comme image.\n"
                                "Utilisez « Copier vers un dossier… » pour copier le fichier.", parent=parent)
            return
        path, _origin = self.library.find_display_file(p, for_thumbnail=False)
        if not path:
            messagebox.showwarning("Copier l'image", "Le fichier de cette photo est introuvable sur le disque.",
                                   parent=parent)
            return
        name = safe_text(p.filename or p.label)
        self._set_status(f"Copie de {name} dans le presse-papiers…")

        def work():
            png = image_to_png(path)
            return copy_png_to_clipboard(png), len(png)

        def done(result, error):
            if error is not None:
                detail = "format d'image non reconnu" if "cannot identify image file" in str(error) else str(error)
                self._set_status(f"Copie de l'image impossible : {detail}")
                messagebox.showerror(
                    "Copier l'image",
                    f"Impossible de placer l'image dans le presse-papiers :\n{detail}\n\n"
                    "Installez le paquet wl-clipboard (Wayland) ou xclip (X11), ou utilisez "
                    "« Copier le chemin du fichier ».", parent=parent)
                return
            self._set_status(f"Image copiée dans le presse-papiers : {name} — collez-la avec Ctrl+V "
                             "dans une autre application.")

        self._run_background(work, done)

    def copy_path(self, p: Photo) -> None:
        path = self.source_file(p) or self.library.to_linux(p.image_path)
        self.root.clipboard_clear()
        self.root.clipboard_append(safe_text(path))
        self._set_status(f"Chemin copié : {safe_text(path)}")

    def _run_background(self, work: Callable, done: Callable) -> None:
        """Exécute work() dans un thread puis done(résultat, erreur) dans le thread Tk."""
        box: list = []

        def runner():
            try:
                box.append((work(), None))
            except Exception as exc:
                box.append((None, exc))

        th = threading.Thread(target=runner, daemon=True)
        th.start()

        def poll():
            if box:
                done(*box[0])
            else:
                self.root.after(60, poll)

        self.root.after(60, poll)

    # -- EXIF ---------------------------------------------------------------------------
    def show_exif(self, p: Photo, parent=None) -> None:
        parent = parent or self.root
        path = self.source_file(p, original=True)
        if not path:
            messagebox.showwarning("Informations EXIF", "Le fichier de cette photo est introuvable sur le disque.",
                                   parent=parent)
            return
        self._set_status(f"Lecture des informations de {safe_text(os.path.basename(path))}…")

        def done(data, error):
            if error is not None:
                data = {"summary": [], "groups": [], "gps": None, "error": str(error)}
            self._set_status("")
            ExifDialog(self, parent, p, path, data)

        self._run_background(lambda: read_exif(path), done)

    # -- copie vers un dossier -------------------------------------------------------------
    def _protected_roots(self) -> list[str]:
        roots = {self.library.root}
        for st in self._mode_states.values():
            lib = st.get("library")
            if lib is not None and lib.root:
                roots.add(lib.root)
        if self.folder.root:
            roots.add(self.folder.root)
        return [r for r in roots if r]

    def copy_photo_to(self, p: Photo, parent=None) -> None:
        name = safe_text(p.filename or p.label)
        self._start_copy([(p, "")], f"« {name} »", "1 image" if p.source == "folder" else "1 photo", "", parent)

    def _start_copy(self, items: list[tuple[Photo, str]], what: str, summary: str, subdir_name: str,
                    parent=None) -> None:
        parent = parent or self.root
        if not items:
            messagebox.showinfo("Copier", "Il n'y a aucune photo à copier.", parent=parent)
            return
        is_library = self.mode == MODE_LIBRARY
        dlg = CopyDialog(self, parent, what, summary, subdir_name, is_library)
        opts = dlg.result
        if not opts:
            return
        sub = safe_filename(opts["subdir"]) if opts["subdir"] else ""
        if sub:
            top = unique_path(opts["dest"], sub)
            sub = os.path.basename(top)
            items = [(ph, os.path.join(sub, rel) if rel else sub) for ph, rel in items]
        lib, original = self.library, opts["original"]
        task = CopyTask(items, lambda ph: resolve_photo_file(lib, ph, original), opts["dest"],
                        self._protected_roots())
        try:
            task.check_destination()
        except OSError as exc:
            messagebox.showerror("Copie impossible", str(exc), parent=parent)
            return
        open_dir = (os.path.join(opts["dest"], sub) if sub else opts["dest"]) if opts["open"] else None
        CopyProgress(self, parent, task, f"Copie de {what}", open_dir)

    def _photos_of_view(self, view: View) -> list[Photo]:
        n = self.photos.count(view)
        return self.photos.page(view, 0, n) if n else []

    def copy_tree_item(self, iid: str) -> None:
        """Copie l'album, l'événement, le dossier ou la liste correspondant à une entrée de l'arborescence."""
        if self.photos is None:
            return
        try:
            spec = self._copy_spec(iid)
        except sqlite3.Error as exc:
            messagebox.showerror("Copier", f"Lecture de l'index impossible : {exc}", parent=self.root)
            return
        if spec is None:
            messagebox.showinfo("Copier", "Sélectionnez d'abord un album, un événement ou un dossier "
                                "dans l'arborescence.", parent=self.root)
            return
        items, what, name = spec
        n = len(items)
        self._start_copy(items, what, plural(n, "photo" if self.mode == MODE_LIBRARY else "image"), name)

    def _copy_spec(self, iid: str) -> Optional[tuple[list[tuple[Photo, str]], str, str]]:
        """(photos avec leur sous-dossier relatif, description, nom du dossier à créer)."""
        if iid == "adv" and self._adv_views.get("adv") is not None:
            v = self._adv_views["adv"]
            if isinstance(v, View):
                return [(p, "") for p in self._photos_of_view(v)], "les résultats de la recherche", "Recherche"
            return ([(p, "") for p in self.folder.photos if path_key(p.image_path) in v[1]],
                    "les résultats de la recherche", "Recherche")
        if self.mode == MODE_FOLDER:
            f = self.folder
            if iid == "fav" or iid.startswith("tag:"):
                sel = "fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])])
                keys = self._annotation_keys(sel)
                items = [(p, "") for p in f.photos if path_key(p.image_path) in keys]
                title = self._annotation_title(sel)
                return items, f"« {title} »", "Favoris" if sel == "fav" else sel[1]
            if not iid.startswith("f:"):
                return None
            rel = iid[2:]
            name = rel.rpartition("/")[2] if rel else (os.path.basename(f.root) or "Dossier")
            items = []
            for p in f.photos:
                if rel and not (p.folder == rel or p.folder.startswith(rel + "/")):
                    continue
                sub = p.folder[len(rel):].lstrip("/") if rel else p.folder
                items.append((p, "/".join(safe_filename(x) for x in sub.split("/")) if sub else ""))
            items.sort(key=lambda it: natural_key(f"{it[1]}/{it[0].filename}"))
            return items, f"le dossier « {safe_text(name)} »", safe_text(name)

        am = self.albums
        if iid == "all":
            return [(p, "") for p in self._photos_of_view(View("all"))], "toutes les photos", "Toutes les photos"
        if iid == "fav" or iid.startswith("tag:"):
            sel = "fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])])
            view = View("keys", keys=tuple(self._annotation_keys(sel)))
            title = self._annotation_title(sel)
            return ([(p, "") for p in self._photos_of_view(view)], f"« {title} »",
                    "Favoris" if sel == "fav" else sel[1])
        if iid == "search":
            view = self._search_view()
            return [(p, "") for p in self._photos_of_view(view)], "les résultats de la recherche", view.title
        if iid.startswith("y:"):
            ys = iid[2:]
            year = None if ys == "none" else int(ys)
            items = []
            for y, events in am.events_by_year():
                if y == year:
                    for e in events:
                        items += [(p, safe_filename(e.name)) for p in self._photos_of_view(View("album", album_id=e.id))]
            label = f"Événements {ys}" if year is not None else "Événements sans date"
            return items, f"les événements de {ys}" if year is not None else "les événements sans date", label
        album_id = self._album_from_iid(iid)
        a = am.get(album_id) if album_id is not None else None
        if a is None:
            return None
        if a.kind == KIND_FOLDER:
            items: list[tuple[Photo, str]] = []

            def walk(folder: Album, rel: str, seen: set[int]) -> None:
                for child in am.children(folder.id):
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    sub = os.path.join(rel, safe_filename(child.name)) if rel else safe_filename(child.name)
                    if child.kind == KIND_FOLDER:
                        walk(child, sub, seen)
                    else:
                        items.extend((p, sub) for p in self._photos_of_view(View("album", album_id=child.id)))

            walk(a, "", {a.id})
            return items, f"le dossier « {a.name} »", a.name
        kind = "l'événement" if a.kind == KIND_EVENT else "l'album"
        return ([(p, "") for p in self._photos_of_view(View("album", album_id=a.id))],
                f"{kind} « {a.name} »", a.name)

    def copy_selected_tree_item(self) -> None:
        tree = self.ftree if self.mode == MODE_FOLDER else self.tree
        sel = tree.selection()
        self.copy_tree_item(sel[0] if sel else "")

    # -- menus contextuels ------------------------------------------------------------------
    def photo_menu(self, p: Photo, parent, x: int, y: int, open_cmd: Optional[Callable] = None) -> None:
        m = tk.Menu(parent, tearoff=False)
        if open_cmd is not None:
            m.add_command(label="  Ouvrir", command=open_cmd, image=self.icons.get("photos", 16), compound="left")
            m.add_separator()
        ic = self.icons
        m.add_command(label="  Copier l'image", command=lambda: self.copy_image(p, parent), accelerator="Ctrl+C",
                      image=ic.get("copy", 16), compound="left")
        m.add_command(label="  Copier le chemin du fichier", command=lambda: self.copy_path(p),
                      accelerator="Ctrl+Maj+C", image=ic.get("file", 16), compound="left")
        m.add_command(label="  Copier vers un dossier…", command=lambda: self.copy_photo_to(p, parent),
                      accelerator="Ctrl+S", image=ic.get("folder_arrow", 16), compound="left")
        m.add_separator()
        fav = self.is_favorite(p)
        m.add_command(label="  Retirer des favoris" if fav else "  Ajouter aux favoris",
                      command=lambda: self.toggle_favorite(p, parent), accelerator="F",
                      image=ic.get("heart_fill" if fav else "heart", 16, "favorite"), compound="left")
        m.add_command(label="  Tags…", command=lambda: self.edit_tags(p, parent), accelerator="T",
                      image=ic.get("tag", 16), compound="left")
        m.add_separator()
        m.add_command(label="  Informations EXIF…", command=lambda: self.show_exif(p, parent), accelerator="I",
                      image=ic.get("info", 16), compound="left")
        if parent is self.root:
            m.add_separator()
            selected = self.is_selected(p)
            m.add_command(label="  Retirer de la sélection" if selected else "  Ajouter à la sélection",
                          accelerator="Ctrl+clic", image=ic.get("check", 16), compound="left",
                          command=lambda: self._toggle_photo_selection(p))
            many = len(self.selection) > 1 and selected
            m.add_command(label=f"  Ajouter {'la sélection' if many else 'à un album virtuel'}…"
                          if not many else f"  Ajouter les {len(self.selection)} photos à un album virtuel…",
                          image=ic.get("album", 16), compound="left",
                          command=lambda: self.add_to_virtual_album(self.selected_photos() if many else [p]))
            if self.mode == MODE_COLLECTIONS and self.view.kind == "valbum" and p.ref:
                m.add_command(label="  Retirer de l'album virtuel (le fichier reste intact)",
                              command=lambda: self._valbum_remove_one(p))
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _toggle_photo_selection(self, p: Photo) -> None:
        k = self.photo_key(p)
        if k in self.selection:
            del self.selection[k]
        else:
            self.selection[k] = p
        self._selection_changed()

    def _valbum_remove_one(self, p: Photo) -> None:
        self.selection = {self.photo_key(p): p}
        self.valbum_remove_selection()

    def _on_canvas_menu(self, event) -> None:
        i = self._index_at(event)
        if i is None:
            return
        if self.view.kind == "timeline":
            return
        self._set_hover(i)
        p = self.page_photos[i]
        absolute = self.page_no * PAGE_SIZE + i
        self.photo_menu(p, self.root, event.x_root, event.y_root, lambda: self.open_viewer(absolute))

    def _hovered_photo(self) -> Optional[Photo]:
        if (self.view.kind != "timeline" and self.hover_index is not None
                and self.hover_index < len(self.page_photos)):
            return self.page_photos[self.hover_index]
        return None

    def _grid_shortcut(self, action: str) -> None:
        """Raccourcis de la fenêtre principale : ils portent sur la vignette survolée."""
        if self._focus_in_entry():
            return
        p = self._hovered_photo()
        if p is None:
            self._set_status("Placez le pointeur sur une vignette pour utiliser ce raccourci.")
            return
        {"copy": self.copy_image, "path": lambda ph: self.copy_path(ph), "to": self.copy_photo_to,
         "fav": self.toggle_favorite, "tags": self.edit_tags, "exif": self.show_exif}[action](p)

    def _on_tree_menu(self, event, tree) -> None:
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid.startswith("smart:") or iid.startswith("saved:"):
            self.smart_menu(iid, event.x_root, event.y_root, tree)
            return
        if self.mode == MODE_FOLDER:
            ok = iid.startswith("f:") or iid in ("fav", "adv") or iid.startswith("tag:")
        else:
            ok = (iid in ("all", "fav", "search", "adv") or iid.startswith("tag:") or iid.startswith("y:")
                  or self._album_from_iid(iid) is not None)
        if not ok:
            return
        label = tree.item(iid, "text").strip()
        if len(label) > 50:
            label = label[:49] + "…"
        m = tk.Menu(tree, tearoff=False)
        m.add_command(label=f"  Copier « {label} » vers un dossier…", command=lambda: self.copy_tree_item(iid),
                      image=self.icons.get("folder_arrow", 16), compound="left")
        if iid.startswith("tag:"):
            name = self._tag_names[int(iid[4:])]
            m.add_separator()
            m.add_command(label=f"Retirer le tag « {name} » de toutes les photos",
                          command=lambda: self.delete_tag(name))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def delete_tag(self, name: str) -> None:
        scope = self._scope()
        keys = self.annotations.keys_with_tag(scope, name)
        if not messagebox.askyesno("Retirer un tag",
                                   f"Retirer le tag « {name} » de {plural(len(keys), 'photo')} ?\n"
                                   "Les photos elles-mêmes ne sont pas modifiées.", parent=self.root):
            return
        try:
            for k in keys:
                self.annotations.set_tags(scope, k, [t for t in self.annotations.tags_of(scope, k) if t != name])
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc)
            return
        if self._annot_sel == ("tag", name):
            self._annot_sel = "fav"
        self._after_annotation_change()

    # =====================================================================================
    # iphotor 2 — sélection multiple, métadonnées, recherche avancée, chronologie,
    # collections, outils d'analyse, dossiers, disques, diagnostic
    # =====================================================================================

    # -- clés --------------------------------------------------------------------------
    def photo_key(self, p: Photo) -> str:
        return "|".join(self._key(p))

    def meta_key(self, p: Photo) -> str:
        """Clé dans metadata.sqlite3 : GUID / « id:N » (bibliothèque) ou chemin (dossier, collections)."""
        if self.mode == MODE_LIBRARY:
            return self._key(p)[1]
        return path_key(p.image_path)

    def meta_path(self) -> str:
        if self.mode == MODE_LIBRARY:
            return os.path.join(self.cache.cache_dir, "metadata.sqlite3")
        return os.path.join(FOLDER_CACHE_DIR, "metadata.sqlite3")

    def meta_index(self) -> "mdlib.MetadataIndex":
        return mdlib.MetadataIndex(self.meta_path(), self._protected_roots())

    def search_dialect(self) -> str:
        return advsearch.LIBRARY if self.mode == MODE_LIBRARY else advsearch.FOLDER

    # -- sélection multiple ------------------------------------------------------------------
    def _selection_changed(self) -> None:
        n = len(self.selection)
        self.sel_label.config(text=plural(n, "photo sélectionnée", "photos sélectionnées") if n else "")
        if n:
            self.selbar_count.config(text=plural(n, "photo sélectionnée", "photos sélectionnées"))
            self.btn_sel_compare.state(["!disabled"] if n == 2 else ["disabled"])
            in_valbum = self.mode == MODE_COLLECTIONS and self.view.kind == "valbum"
            for b in (self.btn_sel_up, self.btn_sel_down, self.btn_sel_remove):
                (b.pack if in_valbum else b.pack_forget)(**({"side": "left", "padx": (2, 0)} if in_valbum else {}))
            if not self.selbar.winfo_ismapped():
                self.selbar.pack(side="top", fill="x", before=self.grid_frame)
        elif self.selbar.winfo_ismapped():
            self.selbar.pack_forget()
        if self.page_photos and self.view.kind != "timeline":
            self._layout()

    def is_selected(self, p: Photo) -> bool:
        return self.photo_key(p) in self.selection

    def toggle_select(self, i: int) -> None:
        p = self.page_photos[i]
        k = self.photo_key(p)
        if k in self.selection:
            del self.selection[k]
        else:
            self.selection[k] = p
        self._sel_anchor = self.page_no * PAGE_SIZE + i
        self._selection_changed()

    def select_range(self, i: int) -> None:
        """Maj+clic : sélectionne de l'ancre jusqu'à la vignette (sur plusieurs pages si besoin)."""
        target = self.page_no * PAGE_SIZE + i
        anchor = self._sel_anchor if self._sel_anchor is not None else target
        lo, hi = min(anchor, target), max(anchor, target)
        try:
            photos = self.photos.page(self.view, lo, hi - lo + 1)
        except sqlite3.Error as exc:
            self._set_status(f"Sélection impossible : {exc}")
            return
        for p in photos:
            self.selection[self.photo_key(p)] = p
        self._selection_changed()

    def select_page(self) -> None:
        if self._focus_in_entry() or self.view.kind == "timeline":
            return
        for p in self.page_photos:
            self.selection[self.photo_key(p)] = p
        self._selection_changed()
        self._set_status(f"{plural(len(self.page_photos), 'photo')} de la page ajoutée(s) à la sélection.")

    def select_view(self) -> None:
        if self.photos is None or self.view.kind in ("empty", "timeline"):
            return
        if self.total > 50000 and not messagebox.askyesno(
                "Sélection", f"Sélectionner les {format_count(self.total)} photos de la vue ?", parent=self.root):
            return
        for p in self.photos.page(self.view, 0, self.total):
            self.selection[self.photo_key(p)] = p
        self._selection_changed()

    def invert_page_selection(self) -> None:
        for p in self.page_photos:
            k = self.photo_key(p)
            if k in self.selection:
                del self.selection[k]
            else:
                self.selection[k] = p
        self._selection_changed()

    def clear_selection(self, _event=None) -> None:
        if _event is not None and self._focus_in_entry():
            return
        if self.selection:
            self.selection.clear()
            self._sel_anchor = None
            self._selection_changed()

    def selected_photos(self) -> list[Photo]:
        return list(self.selection.values())

    # -- actions groupées --------------------------------------------------------------------
    def batch_favorite(self, on: bool) -> None:
        photos = self.selected_photos()
        try:
            for p in photos:
                self.annotations.set_favorite(*self._key(p), on)
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc)
            return
        self._set_status(f"{plural(len(photos), 'photo')} {'ajoutée(s) aux' if on else 'retirée(s) des'} favoris.")
        self._after_annotation_change()

    def batch_add_tags(self) -> None:
        photos = self.selected_photos()
        if not photos:
            return
        d = TextDialog(self, self.root, "Ajouter des tags", f"Ajouter des tags à {plural(len(photos), 'photo')}",
                       "Tags (séparés par des virgules)", icon="tag", ok_text="Ajouter")
        if not d.result:
            return
        new = [t for t in (clean_tag(x) for x in d.result.split(",")) if t]
        try:
            for p in photos:
                scope, key = self._key(p)
                cur = self.annotations.tags_of(scope, key)
                self.annotations.set_tags(scope, key, cur + [t for t in new if t not in cur])
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc)
            return
        self._set_status(f"Tags {', '.join(new)} ajoutés à {plural(len(photos), 'photo')}.")
        self._after_annotation_change()

    def batch_remove_tag(self) -> None:
        photos = self.selected_photos()
        present = sorted({t for p in photos for t in self.annotations.tags_of(*self._key(p))}, key=normalize_text)
        if not present:
            messagebox.showinfo("Retirer un tag", "Aucune photo sélectionnée ne porte de tag.", parent=self.root)
            return
        d = TextDialog(self, self.root, "Retirer un tag", "Retirer un tag de la sélection",
                       f"Tag à retirer ({', '.join(present[:12])}{'…' if len(present) > 12 else ''})",
                       present[0], icon="tag", ok_text="Retirer")
        if not d.result:
            return
        name = clean_tag(d.result)
        n = 0
        try:
            for p in photos:
                scope, key = self._key(p)
                cur = self.annotations.tags_of(scope, key)
                if name in cur:
                    self.annotations.set_tags(scope, key, [t for t in cur if t != name])
                    n += 1
        except (sqlite3.Error, OSError) as exc:
            self._annotation_error(exc)
            return
        self._set_status(f"Tag « {name} » retiré de {plural(n, 'photo')}.")
        self._after_annotation_change()

    def batch_copy(self) -> None:
        photos = self.selected_photos()
        self._start_copy([(p, "") for p in photos], plural(len(photos), "photo sélectionnée", "photos sélectionnées"),
                         plural(len(photos), "photo"), "")

    def batch_properties(self) -> None:
        photos = self.selected_photos()
        if not photos:
            return
        keys = [self.meta_key(p) for p in photos]
        metas = self.meta_records(keys)
        dates = [p.date for p in photos if p.date is not None]
        sizes = [m.get("file_size") for m in metas.values() if m.get("file_size")]
        favs = sum(1 for p in photos if self.is_favorite(p))
        tag_sets = [set(self.tags_of(p)) for p in photos]
        common_tags = set.intersection(*tag_sets) if tag_sets else set()

        def common(field):
            vals = {metas[k].get(field) for k in keys if k in metas}
            vals.discard(None)
            vals.discard("")
            if len(vals) == 1:
                return str(vals.pop())
            return "(varie)" if vals else "—"
        rows = [("#", "Sélection"), ("Photos", format_count(len(photos))),
                ("Vidéos", format_count(sum(1 for p in photos if p.is_video))),
                ("Favoris", format_count(favs)),
                ("Tags communs", ", ".join(sorted(common_tags, key=normalize_text)) or "—"),
                ("Période", f"{format_date(min(dates), False)} → {format_date(max(dates), False)}" if dates else "—"),
                ("Taille totale", (format_size(sum(sizes)) + (f" ({len(sizes)} fichiers connus)" if len(sizes) < len(photos)
                                                             else "")) if sizes else "inconnue (métadonnées non analysées)"),
                ("#", "Propriétés communes (métadonnées analysées)"),
                ("Analysées", f"{len(metas)} / {len(photos)}"),
                ("Fabricant", common("make")), ("Appareil", common("model")), ("Objectif", common("lens")),
                ("ISO", common("iso")), ("Focale", common("focal")), ("Ouverture", common("aperture")),
                ("Largeur", common("width")), ("Hauteur", common("height"))]
        events = {p.event_name for p in photos if p.event_name}
        if events:
            rows.insert(6, ("Événements", ", ".join(sorted(events))[:200]))
        InfoDialog(self, self.root, "Propriétés", "Propriétés de la sélection", plural(len(photos), "photo"), rows)

    def add_to_virtual_album(self, photos: Optional[list] = None) -> None:
        photos = photos if photos is not None else self.selected_photos()
        if not photos:
            return
        d = AlbumChooserDialog(self, self.root, len(photos))
        if d.result is None:
            return
        refs = [self.photo_ref(p) for p in photos]
        try:
            n = self.collections.add(d.result, refs)
        except (sqlite3.Error, OSError) as exc:
            messagebox.showerror("Album virtuel", str(exc), parent=self.root)
            return
        a = self.collections.get(d.result)
        self._set_status(f"{plural(n, 'photo ajoutée', 'photos ajoutées')} à l'album virtuel « {a.name if a else ''} »"
                         + (f" ({len(refs) - n} déjà présente(s))" if n < len(refs) else "") + ".")
        if self.mode == MODE_COLLECTIONS:
            self._populate_ctree()

    def photo_ref(self, p: Photo) -> dict:
        scope, key = self._key(p)
        path = self.source_file(p) or self.library.to_linux(p.image_path) or p.image_path
        try:
            size = os.path.getsize(path)
        except (OSError, ValueError):
            size = p.size
        return {"scope": scope, "key": key, "path": path, "filename": p.filename, "size": size, "date": p.date}

    # -- index des métadonnées ------------------------------------------------------------
    def _metadata_items(self):
        """(itérateur à exécuter dans un thread, total) pour la source courante."""
        if self.mode == MODE_LIBRARY:
            index_path, lib = self.cache.index_path, self.library
            total = self.total_photos()

            def gen():
                con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    for r in mdlib.iter_rows(con, f"SELECT {PhotoManager.COLUMNS} FROM photos p ORDER BY p.id"):
                        ph = PhotoManager._photo(r)
                        yield (ph.guid or f"id:{ph.id}", ph.id, (lambda ph=ph: resolve_photo_file(lib, ph, True)))
                finally:
                    con.close()
            return gen(), total
        photos = list(self.photos.photos) if self.photos is not None and hasattr(self.photos, "photos") else []
        return ((path_key(p.image_path), p.id, (lambda p=p: p.image_path if os.path.isfile(p.image_path) else None))
                for p in photos), len(photos)

    def total_photos(self) -> int:
        if self.mode == MODE_LIBRARY:
            if self.con is None:
                return 0
            return self.con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        return len(self.photos.photos) if self.photos is not None and hasattr(self.photos, "photos") else 0

    def metadata_coverage(self) -> tuple[int, int, int]:
        """(photos de la source, photos analysées, dont avec EXIF)."""
        total = self.total_photos()
        if not total or not os.path.isfile(self.meta_path()):
            return total, 0, 0
        try:
            idx = self.meta_index()
            if self.mode == MODE_LIBRARY:
                con = idx.connect()
                try:
                    r = con.execute("SELECT SUM(status = 'ok'), SUM(has_exif) FROM photo_meta").fetchone()
                finally:
                    con.close()
                return total, int(r[0] or 0), int(r[1] or 0)
            keys = [path_key(p.image_path) for p in self.photos.photos]
            n, ex = idx.coverage(keys)
            return total, n, ex
        except (sqlite3.Error, OSError, PermissionError):
            return total, 0, 0

    def analyze_metadata(self, parent=None, done: Optional[Callable] = None) -> None:
        """Construit / met à jour l'index des métadonnées (incrémental, reprenable, annulable)."""
        if self.photos is None or self.total_photos() == 0:
            messagebox.showinfo("Métadonnées", "Ouvrez d'abord une bibliothèque ou un dossier.", parent=parent or self.root)
            return
        if self.mode == MODE_LIBRARY and not self.library.is_available():
            messagebox.showwarning("Métadonnées", "La bibliothèque est inaccessible (disque débranché ?) : "
                                   "les fichiers ne peuvent pas être lus.", parent=parent or self.root)
            return
        try:
            idx = self.meta_index()
        except PermissionError as exc:
            messagebox.showerror("Métadonnées", str(exc), parent=parent or self.root)
            return
        items, total = self._metadata_items()
        prog = mdlib.IndexProgress()

        def work(task):
            task.total = total
            th = threading.Thread(target=idx.update, args=(items, prog, task.cancel, total), daemon=True)
            th.start()
            while th.is_alive():
                task.done = prog.done
                rate = prog.done / max(0.5, time.time() - prog.started)
                eta = (total - prog.done) / rate if rate > 0 else 0
                task.text = (f"{format_count(prog.done)} / {format_count(total)} fichiers — {format_count(prog.updated)} "
                             f"lus, {format_count(prog.skipped)} déjà à jour"
                             + (f", {prog.missing} introuvables" if prog.missing else "")
                             + (f" — reste ≈ {int(eta // 60)} min {int(eta % 60)} s" if prog.done > 20 and eta > 5 else ""))
                th.join(0.2)
            return prog

        def finished(p, error):
            if error is not None or (p and p.fatal):
                messagebox.showerror("Métadonnées", f"Analyse interrompue : {error or p.fatal}", parent=parent or self.root)
            elif p:
                msg = (f"Métadonnées : {format_count(p.updated)} fichier(s) analysé(s), {format_count(p.skipped)} déjà à jour"
                       + (f", {p.missing} introuvable(s)" if p.missing else "")
                       + (f", {p.errors} illisible(s)" if p.errors else "")
                       + (" — analyse annulée (elle reprendra là où elle s'est arrêtée)." if p.cancelled else "."))
                self._set_status(msg)
            self._attach_meta()
            if done:
                done()

        TaskDialog(self, parent or self.root, "Analyse des métadonnées",
                   f"Analyse des métadonnées de {plural(total, 'photo')} (EXIF, dimensions, GPS)", work, finished)

    def _attach_meta(self) -> None:
        if self.con is not None and self.mode == MODE_LIBRARY:
            try:
                advsearch.install_functions(self.con)
                advsearch.attach_meta(self.con, self.meta_path())
            except sqlite3.Error:
                pass

    def meta_records(self, keys: list) -> dict:
        """Métadonnées analysées (dict par clé) ; {} si l'index n'existe pas."""
        if not keys or not os.path.isfile(self.meta_path()):
            return {}
        try:
            con = self.meta_index().connect()
            try:
                out = {}
                for i in range(0, len(keys), 900):
                    chunk = keys[i:i + 900]
                    for r in con.execute(f"SELECT * FROM photo_meta WHERE status = 'ok' AND key IN "
                                         f"({','.join('?' * len(chunk))})", chunk):
                        out[r["key"]] = dict(r)
                return out
            finally:
                con.close()
        except (sqlite3.Error, OSError):
            return {}

    def meta_record(self, key: str, path: str) -> Optional[dict]:
        rec = self.meta_records([key]).get(key) if key else None
        if rec is None and path:
            rec = self.meta_records([path_key(path)]).get(path_key(path))
        return rec

    def apple_terms_text(self, p: Photo) -> str:
        if self.mode != MODE_LIBRARY or not os.path.isfile(self.meta_path()):
            return ""
        try:
            terms = self.meta_index().terms_of(self.meta_key(p))
        except (sqlite3.Error, OSError):
            return ""
        parts = []
        for kind, label in (("keyword", "Mots-clés"), ("person", "Personnes"), ("place", "Lieu")):
            if terms.get(kind):
                parts.append(f"{label} : {', '.join(terms[kind])}")
        return "   ·   ".join(parts)

    # -- annotations pour la recherche ------------------------------------------------------
    def annotation_source(self):
        scope = self._scope()
        ann = self.annotations

        class Snap:
            def __init__(s):
                s.fav = ann.favorites(scope)
                s.tags = {t: ann.keys_with_tag(scope, t) for t in ann.all_tags(scope)}

            def favorites(s):
                return s.fav

            def keys_with_tag(s, t):
                t = clean_tag(t)
                return next((v for k, v in s.tags.items() if normalize_text(k) == normalize_text(t)), set())

            def tagged_keys(s):
                out = set()
                for v in s.tags.values():
                    out |= v
                return out
        return Snap()

    def known_tags(self) -> list[str]:
        try:
            return self.annotations.all_tags(self._scope())
        except (sqlite3.Error, OSError):
            return []

    # -- recherche avancée -----------------------------------------------------------------
    def open_advanced_search(self, criteria: Optional[dict] = None) -> None:
        if self.mode == MODE_COLLECTIONS:
            messagebox.showinfo("Recherche avancée", "La recherche avancée porte sur une bibliothèque ou un dossier. "
                                "Passez en mode Bibliothèque ou Dossier.", parent=self.root)
            return
        if self.photos is None:
            messagebox.showinfo("Recherche avancée", "Ouvrez d'abord une bibliothèque ou un dossier.", parent=self.root)
            return
        if self._adv_dialog is not None and self._adv_dialog.win.winfo_exists():
            self._adv_dialog.win.lift()
            if criteria:
                self._adv_dialog.load(criteria)
            return
        self._adv_dialog = AdvancedSearchDialog(self, criteria)

    def run_advanced_search(self, criteria: dict, title: str = "Recherche avancée",
                            done: Optional[Callable] = None, tree_iid: str = "adv") -> None:
        try:
            compiled = advsearch.compile_criteria(criteria, self.search_dialect(), self.annotation_source())
        except advsearch.CriteriaError as exc:
            messagebox.showwarning("Recherche avancée", str(exc), parent=self.root)
            return
        meta_path = self.meta_path()
        mode = self.mode
        if mode == MODE_LIBRARY:
            index_path = self.cache.index_path
            sql = advsearch.build_query(compiled, advsearch.LIBRARY, True)

            def work():
                con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
                try:
                    advsearch.install_functions(con)
                    advsearch.attach_meta(con, meta_path)
                    return [r[0] for r in con.execute(sql, compiled.params)]
                finally:
                    con.close()
        else:
            photos = list(self.folder.photos)
            sql = advsearch.build_query(compiled, advsearch.FOLDER, True)

            def work():
                con = sqlite3.connect(":memory:")
                try:
                    advsearch.install_functions(con)
                    advsearch.attach_meta(con, meta_path)
                    advsearch.folder_table(con, photos, lambda p: path_key(p.image_path))
                    return [r[0] for r in con.execute(sql, compiled.params)]
                finally:
                    con.close()
        self._set_status("Recherche en cours…")
        t0 = time.time()

        def finished(ids, error):
            if error is not None:
                messagebox.showerror("Recherche avancée", f"La recherche a échoué :\n{error}", parent=self.root)
                return
            if self.mode != mode:
                return
            self.adv_criteria = criteria
            self.show_ids(ids, title, tree_iid=tree_iid, tree_label=title, desc=advsearch.describe(criteria))
            note = ""
            if compiled.uses_meta:
                total, analysed, _ex = self.metadata_coverage()
                if analysed < total:
                    note = f" — {format_count(total - analysed)} photo(s) non analysée(s) exclue(s) des critères EXIF"
            self._set_status(f"{title} : {plural(len(ids), 'photo')} en {time.time() - t0:.2f} s{note}."
                             + (" " + compiled.warnings[0] if compiled.warnings else ""))
            if done:
                done(len(ids), compiled.warnings + ([note.strip(' —')] if note else []))

        self._run_background(work, finished)

    def show_ids(self, ids: list, title: str, tree_iid: str = "adv", tree_label: str = "Recherche avancée",
                 desc: str = "") -> None:
        """Affiche une liste d'identifiants dans la grille (et une entrée dans l'arborescence)."""
        self._annot_sel = None
        self.current_album_id = None
        if self.mode == MODE_LIBRARY:
            view = View("ids", ids=tuple(ids), title=title, query=desc)
            self._adv_views[tree_iid] = view
            self._insert_result_entry(self.tree, tree_iid, tree_label, len(ids))
            self.set_view(view)
        else:
            f = self.folder
            f.subdir, f.date_range = None, None
            f.only = {path_key(f.photos[i].image_path) for i in ids if 0 <= i < len(f.photos)}
            f.only_title = f"{title} : {desc}" if desc else title
            f.refresh()
            self.photos = f
            self._adv_views[tree_iid] = ("folder", set(f.only), f.only_title)
            self._insert_result_entry(self.ftree, tree_iid, tree_label, len(ids))
            self.set_view(self._folder_view())

    def _insert_result_entry(self, tree, iid: str, label: str, n: int) -> None:
        if iid != "adv":
            self._select_silently(tree, iid) if tree.exists(iid) else None
            return
        if tree.exists(iid):
            tree.delete(iid)
        tree.insert("", 0, iid=iid, text=f" {label}", values=(format_count(n),), tags=("section",),
                    image=self.icons.get("search", 16, "accent"))
        self._select_silently(tree, iid)

    def show_keys_in_grid(self, keys: set, title: str) -> None:
        """Affiche des photos désignées par leurs clés de métadonnées (doublons, carte)."""
        if self.mode == MODE_LIBRARY:
            ids = []
            for k in keys:
                if k.startswith("id:"):
                    ids.append(int(k[3:]))
            guids = [k for k in keys if not k.startswith("id:")]
            for i in range(0, len(guids), 900):
                chunk = guids[i:i + 900]
                ids += [r[0] for r in self.con.execute(
                    f"SELECT id FROM photos WHERE guid IN ({','.join('?' * len(chunk))})", chunk)]
            self.show_ids(ids, title, tree_iid="adv", tree_label="Résultat")
        elif self.mode == MODE_FOLDER:
            ids = [p.id for p in self.folder.photos if path_key(p.image_path) in keys]
            self.show_ids(ids, title, tree_iid="adv", tree_label="Résultat")
        self.root.lift()

    def photo_from_path(self, path: str) -> Photo:
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        return Photo(id=-1, guid="", caption="", comment="", rating=0, roll_id=None, media_type="Image",
                     image_path=path, original_path="", thumb_path="", date=unix_to_interval(mtime) if mtime else None,
                     mod_date=mtime, filename=os.path.basename(path), size=size, source="folder")

    # -- albums intelligents et recherches enregistrées --------------------------------------
    def refresh_smart_entries(self) -> None:
        if self.mode == MODE_LIBRARY:
            t = self.tree
            if self.photos is None or not t.exists("all"):
                return
        elif self.mode == MODE_FOLDER:
            t = self.ftree
            if not self.folder.root or not t.exists("f:"):
                return
        else:
            return
        try:
            smart = self.collections.albums("smart")
            saved = self.collections.searches()
        except (sqlite3.Error, OSError, RuntimeError):
            return
        for iid in ("sec:smart", "sec:saved"):
            if t.exists(iid):
                t.delete(iid)
        anchor = "sec:tags" if t.exists("sec:tags") else "fav"
        pos = t.index(anchor) + 1 if t.exists(anchor) else 1
        if smart:
            t.insert("", pos, iid="sec:smart", text=" Albums intelligents", values=(format_count(len(smart)),),
                     tags=("section",), image=self.icons.get("smart", 16, "accent"), open=True)
            for a in smart:
                t.insert("sec:smart", "end", iid=f"smart:{a.id}", text=" " + a.name, values=("",),
                         image=self.icons.get("smart", 16))
            pos += 1
        if saved:
            t.insert("", pos, iid="sec:saved", text=" Recherches enregistrées", values=(format_count(len(saved)),),
                     tags=("section",), image=self.icons.get("search", 16, "accent"), open=False)
            for sid, name, _c in saved:
                t.insert("sec:saved", "end", iid=f"saved:{sid}", text=" " + name, values=("",),
                         image=self.icons.get("search", 16))

    def open_smart(self, album_id: int) -> None:
        a = self.collections.get(album_id)
        if a is None or not a.criteria:
            return
        self.run_advanced_search(a.criteria, title=f"Album intelligent « {a.name} »", tree_iid=f"smart:{album_id}")

    def open_saved_search(self, search_id: int) -> None:
        for sid, name, crit in self.collections.searches():
            if sid == search_id:
                self.run_advanced_search(crit, title=f"Recherche « {name} »", tree_iid=f"saved:{sid}")
                return

    def smart_menu(self, iid: str, x: int, y: int, tree) -> None:
        m = tk.Menu(tree, tearoff=False)
        if iid.startswith("smart:"):
            aid = int(iid[6:])
            a = self.collections.get(aid)
            m.add_command(label="  Modifier les critères…", command=lambda: AdvancedSearchDialog(self, a.criteria, aid),
                          image=self.icons.get("search", 16), compound="left")
            m.add_command(label="  Renommer…", command=lambda: self._rename_valbum(aid))
            m.add_separator()
            m.add_command(label="  Supprimer l'album intelligent", command=lambda: self._delete_valbum(aid))
        elif iid.startswith("saved:"):
            sid = int(iid[6:])
            crit = next((c for i, _n, c in self.collections.searches() if i == sid), None)
            m.add_command(label="  Ouvrir dans la recherche avancée…", command=lambda: self.open_advanced_search(crit))
            m.add_separator()
            m.add_command(label="  Supprimer la recherche", command=lambda: (self.collections.delete_search(sid),
                                                                             self.refresh_smart_entries()))
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    # -- chronologie ---------------------------------------------------------------------------
    def _timeline_data(self) -> "tl.Timeline":
        key = (self.mode, self.library.root, self.total_photos())
        if self._timeline is None or self._timeline_key != key:
            if self.mode == MODE_LIBRARY:
                rows = self.con.execute("SELECT id, date FROM photos").fetchall()
                items = [(r[0], storage.apple_to_unix(r[1])) for r in rows]
            else:
                items = [(p.id, storage.apple_to_unix(p.date)) for p in self.folder.photos]
            self._timeline = tl.Timeline.build(items)
            valid = {i for i, ts in items if tl._valid(ts) is not None}
            self._tl_unknown = [i for i, _t in items if i not in valid]
            self._timeline_key = key
        return self._timeline

    def show_timeline(self, level: str = "years", year: Optional[int] = None, month: Optional[int] = None,
                      push: bool = True) -> None:
        if self.photos is None or self.mode == MODE_COLLECTIONS:
            return
        if push and self.view.kind in ("timeline", "date") and level != "years":
            self._tl_stack.append(self.view)
        elif level == "years":
            self._tl_stack = []
        t = self._timeline_data()
        if level == "years":
            title = "Chronologie"
        elif level == "months":
            title = f"Chronologie · {year}"
        else:
            title = f"Chronologie · {tl.MONTHS_FR[month - 1].capitalize()} {year}"
        self._annot_sel = None
        self.folder.date_range = None
        self.set_view(View("timeline", level=level, year=year, month=month, title=title))

    def _timeline_periods(self) -> list:
        t = self._timeline_data()
        v = self.view
        if v.level == "years":
            return t.year_periods()
        if v.level == "months":
            return t.month_periods(v.year)
        return t.day_periods(v.year, v.month)

    def _timeline_photo(self, pid: Optional[int]) -> Optional[Photo]:
        if pid is None:
            return None
        return self.photos.get(pid) if self.mode == MODE_LIBRARY else self.folder.get(pid)

    def open_period(self, per) -> None:
        v = self.view
        if per.level == "year":
            self.show_timeline("months", per.year)
        elif per.level == "month" and per.count:
            self.show_timeline("days", per.year, per.month)
        elif per.level == "day":
            self._tl_stack.append(v)
            start, end = storage.unix_to_apple(per.start), storage.unix_to_apple(per.end)
            title = f"Photos du {per.label}"
            if self.mode == MODE_LIBRARY:
                self.set_view(View("date", start=start, end=end, title=title))
            else:
                f = self.folder
                f.only, f.subdir = None, None
                f.date_range = (start, end)
                f.range_title = title
                f.refresh()
                self.photos = f
                self.set_view(self._folder_view())
        elif per.level == "unknown":
            self._tl_stack.append(v)
            self.show_ids(self._tl_unknown, "Photos sans date exploitable", tree_iid="timeline")
        self._update_back()

    def timeline_back(self) -> None:
        if not self._tl_stack:
            self.show_timeline("years", push=False)
            return
        prev = self._tl_stack.pop()
        self.folder.date_range = None
        if prev.kind == "timeline":
            self.set_view(prev)
        self._update_back()

    def _update_back(self) -> None:
        show = (self.view.kind == "timeline" and self.view.level != "years") or bool(self._tl_stack)
        show = show and self._tree_current() == "timeline"
        if show and not self.btn_back.winfo_ismapped():
            self.btn_back.pack(side="left", padx=(0, SPACE_M), anchor="n", before=self.header_left)
        elif not show and self.btn_back.winfo_ismapped():
            self.btn_back.pack_forget()

    def _tree_current(self) -> str:
        t = {MODE_LIBRARY: self.tree, MODE_FOLDER: self.ftree}.get(self.mode, getattr(self, "ctree", self.tree))
        sel = t.selection()
        return sel[0] if sel else ""

    def _draw_period(self, i: int, per) -> None:
        c = self.canvas
        x, y = self._tile_origin(i)
        tag = f"t{i}"
        empty = per.level in ("gap",) or (per.level == "month" and per.count == 0)
        c.create_image(x, y, image=self.card_image("hover" if self.hover_index == i and not empty else "normal"),
                       anchor="nw", tags=(tag, f"bg{i}"))
        wx, wy = x + (TILE_W - THUMB_SIZE) // 2, y + 12
        cx, cy = wx + THUMB_SIZE // 2, wy + THUMB_SIZE // 2
        c.create_image(wx, wy, image=self.well_image(), anchor="nw", tags=(tag,))
        state = self.tile_images.get(i)
        if isinstance(state, dict) and state.get("img") is not None:
            c.create_image(cx, cy, image=state["img"], tags=(tag,))
        else:
            icon = "event" if per.level != "unknown" else "warning"
            c.create_image(cx, cy - (8 if empty else 0), image=self.icons.get(icon, 32, "faint"), tags=(tag,))
            if empty:
                c.create_text(cx, cy + 24, text="Aucune photo", fill=COLORS["muted"], font=self.font_small, tags=(tag,))
        if per.top and per.count:
            w = self.font_badge.measure("LA PLUS RICHE") + 30
            c.create_image(wx + 6, wy + 6, image=self.pill_image(w, 20), anchor="nw", tags=(tag,))
            c.create_image(wx + 15, wy + 16, image=self.icons.get("star_fill", 10, "star"), tags=(tag,))
            c.create_text(wx + 24, wy + 16, text="LA PLUS RICHE", fill="#FFFFFF", font=self.font_badge, anchor="w",
                          tags=(tag,))
        ty = wy + THUMB_SIZE + 10
        c.create_text(x + 14, ty, text=self._truncate(per.label, self.font_bold, TILE_W - 28), anchor="nw",
                      fill=COLORS["faint"] if empty else COLORS["text"], font=self.font_bold, tags=(tag,))
        sub = ("Période sans photo" if per.level == "gap" else plural(per.count, "photo"))
        c.create_text(x + 14, ty + 19, text=sub, anchor="nw", fill=COLORS["muted"], font=self.font_small, tags=(tag,))

    # -- collections (albums virtuels) --------------------------------------------------------
    def _populate_ctree(self) -> None:
        t = self.ctree
        sel = t.selection()
        t.delete(*t.get_children())
        try:
            albums = self.collections.albums("manual")
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            t.insert("", "end", text=f" Collections illisibles : {exc}", tags=("error",),
                     image=self.icons.get("warning", 16, "missing"))
            return
        t.insert("", "end", iid="sec:valbums", text=" Albums virtuels", values=(format_count(len(albums)),),
                 tags=("section",), open=True, image=self.icons.get("album", 16, "accent"))
        for a in albums:
            t.insert("sec:valbums", "end", iid=f"va:{a.id}", text=" " + a.name, values=(format_count(a.count),),
                     image=self.icons.get("album", 16), tags=("empty",) if a.count == 0 else ())
        if not albums:
            t.insert("sec:valbums", "end", iid="va:none", text="Aucun album : sélectionnez des photos puis "
                     "« Album virtuel… »", tags=("empty",))
        if sel and t.exists(sel[0]):
            self._select_silently(t, sel[0])

    def _on_ctree_select(self, _event=None) -> None:
        if self.mode != MODE_COLLECTIONS or self._tree_silent:
            return
        sel = self.ctree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("va:") and iid != "va:none":
            self.open_valbum(int(iid[3:]))
        elif iid == "sec:valbums":
            self.set_view(View("empty", title="Albums virtuels"),
                          message="Un album virtuel regroupe des photos de plusieurs bibliothèques et dossiers, "
                                  "sans copier ni déplacer les fichiers.\n\nSélectionnez des photos (Ctrl+clic), puis "
                                  "« Album virtuel… » dans la barre de sélection.")

    def open_valbum(self, album_id: int) -> None:
        a = self.collections.get(album_id)
        if a is None:
            return
        items = self.collections.items(album_id)
        b = CollectionBrowser()
        b.load_items(items)
        self.coll_browser = b
        self.library.set_items(items)
        self.photos = b
        self.current_valbum = album_id
        self.set_view(View("valbum", album_id=album_id, title=f"Album virtuel : {a.name}"))

    def valbum_menu(self, iid: str, x: int, y: int) -> None:
        m = tk.Menu(self.ctree, tearoff=False)
        ic = self.icons
        m.add_command(label="  Nouvel album virtuel…", command=self.new_valbum, image=ic.get("album", 16), compound="left")
        if iid.startswith("va:") and iid != "va:none":
            aid = int(iid[3:])
            m.add_separator()
            m.add_command(label="  Renommer…", command=lambda: self._rename_valbum(aid))
            m.add_command(label="  Propriétés", command=lambda: self._valbum_properties(aid),
                          image=ic.get("info", 16), compound="left")
            m.add_command(label="  Retrouver les photos introuvables…", command=lambda: self._relocate_valbum(aid),
                          image=ic.get("search", 16), compound="left")
            m.add_separator()
            m.add_command(label="  Diaporama", command=lambda: (self.open_valbum(aid), self.start_slideshow("view")))
            m.add_command(label="  Copier les photos vers un dossier…",
                          command=lambda: self._copy_valbum(aid), image=ic.get("folder_arrow", 16), compound="left")
            m.add_command(label="  Exporter les métadonnées…", command=lambda: (self.open_valbum(aid),
                                                                                ExportDialog(self, self.root, "view")))
            m.add_separator()
            m.add_command(label="  Supprimer l'album (les photos ne sont pas supprimées)",
                          command=lambda: self._delete_valbum(aid))
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def new_valbum(self) -> None:
        d = TextDialog(self, self.root, "Nouvel album virtuel", "Nouvel album virtuel", "Nom de l'album",
                       icon="album", ok_text="Créer")
        if d.result:
            aid = self.collections.create(d.result)
            if self.mode == MODE_COLLECTIONS:
                self._populate_ctree()
                self.ctree.selection_set(f"va:{aid}")

    def _rename_valbum(self, aid: int) -> None:
        a = self.collections.get(aid)
        if a is None:
            return
        d = TextDialog(self, self.root, "Renommer", "Renommer l'album", "Nouveau nom", a.name, icon="album",
                       ok_text="Renommer")
        if d.result:
            self.collections.rename(aid, d.result)
            self._populate_ctree() if self.mode == MODE_COLLECTIONS else self.refresh_smart_entries()

    def _delete_valbum(self, aid: int) -> None:
        a = self.collections.get(aid)
        if a is None:
            return
        what = "l'album intelligent" if a.kind == "smart" else "l'album virtuel"
        if not messagebox.askyesno("Supprimer", f"Supprimer {what} « {a.name} » ?\n\nSeules les références sont "
                                   "supprimées : aucune photo n'est effacée.", parent=self.root):
            return
        self.collections.delete(aid)
        if a.kind == "smart":
            self.refresh_smart_entries()
        else:
            self._populate_ctree()
            if self.current_valbum == aid:
                self.current_valbum = None
                self.photos = None
                self.set_view(View("empty", title="Albums virtuels"))

    def _valbum_properties(self, aid: int) -> None:
        a = self.collections.get(aid)
        items = self.collections.items(aid)
        scopes: dict[str, int] = {}
        for i in items:
            label = "Dossiers" if i.scope == FILES_SCOPE else os.path.basename(i.scope[4:]) or i.scope
            scopes[label] = scopes.get(label, 0) + 1
        missing = [i for i in items if not i.exists]
        rows = [("#", "Album virtuel"), ("Nom", a.name), ("Photos", format_count(len(items))),
                ("Introuvables", format_count(len(missing)), "warn" if missing else ""),
                ("Créé le", format_date(storage.unix_to_apple(a.created)) if a.created else "—"),
                ("Modifié le", format_date(storage.unix_to_apple(a.modified)) if a.modified else "—"),
                ("#", "Sources")] + [(k, format_count(v)) for k, v in sorted(scopes.items())]
        if missing:
            rows += [("#", "Références introuvables (extrait)")] + [(i.filename, safe_text(i.path), "warn")
                                                                     for i in missing[:30]]
        InfoDialog(self, self.root, "Album virtuel", a.name, "Les albums virtuels ne contiennent que des références.",
                   rows, icon="album")

    def _relocate_valbum(self, aid: int) -> None:
        missing = [i for i in self.collections.items(aid) if not i.exists]
        if not missing:
            messagebox.showinfo("Retrouver", "Toutes les photos de cet album sont accessibles.", parent=self.root)
            return
        folder = filedialog.askdirectory(parent=self.root, mustexist=True,
                                         title=f"Dossier où chercher les {len(missing)} photo(s) introuvable(s)")
        if not folder:
            return
        old_common = os.path.commonpath([os.path.dirname(i.path) for i in missing if i.path]) if all(
            i.path for i in missing) else ""

        def work(task):
            task.text = "Recherche par nom et taille (lecture seule)…"
            n = 0
            if old_common and old_common != "/":
                n = self.collections.relocate(aid, old_common, folder)
            return n + self.collections.find_by_name(aid, folder, cancelled=task.cancel.is_set)

        def done(n, error):
            if error is not None:
                messagebox.showerror("Retrouver", str(error), parent=self.root)
                return
            left = sum(1 for i in self.collections.items(aid) if not i.exists)
            messagebox.showinfo("Retrouver", f"{plural(n or 0, 'référence retrouvée', 'références retrouvées')}."
                                + (f"\n{left} photo(s) restent introuvables." if left else ""), parent=self.root)
            self._populate_ctree()
            if self.current_valbum == aid:
                self.open_valbum(aid)

        TaskDialog(self, self.root, "Retrouver", "Recherche des photos introuvables", work, done, icon="search")

    def _copy_valbum(self, aid: int) -> None:
        a = self.collections.get(aid)
        self.open_valbum(aid)
        photos = self.coll_browser.page(None, 0, self.coll_browser.count())
        self._start_copy([(p, "") for p in photos], f"l'album virtuel « {a.name} »", plural(len(photos), "photo"), a.name)

    def valbum_remove_selection(self) -> None:
        if self.current_valbum is None:
            return
        refs = [p.ref for p in self.selected_photos() if p.ref]
        n = self.collections.remove(self.current_valbum, refs)
        self.clear_selection()
        self._set_status(f"{plural(n, 'photo retirée', 'photos retirées')} de l'album virtuel (fichiers intacts).")
        self.open_valbum(self.current_valbum)
        self._populate_ctree()

    def valbum_move_selection(self, delta: int) -> None:
        if self.current_valbum is None:
            return
        refs = [p.ref for p in self.selected_photos() if p.ref]
        self.collections.move(self.current_valbum, refs, delta)
        keep = set(refs)
        self.open_valbum(self.current_valbum)
        self.selection = {self.photo_key(p): p for p in self.coll_browser.photos if p.ref in keep}
        self._selection_changed()

    # -- portées (sélection / vue / source) ------------------------------------------------------
    def scope_photos(self, scope: str) -> list[Photo]:
        if scope == "selection":
            return self.selected_photos()
        if self.photos is None:
            return []
        if scope == "view":
            if self.view.kind in ("empty", "timeline"):
                return []
            return self.photos.page(self.view, 0, self.total)
        if self.mode == MODE_LIBRARY:
            return self.photos.page(View("all"), 0, self.total_photos())
        return list(getattr(self.photos, "photos", []))

    def scope_count(self, scope: str) -> int:
        if scope == "selection":
            return len(self.selection)
        if scope == "view":
            return 0 if self.view.kind in ("empty", "timeline") else self.total
        return self.total_photos()

    def build_records(self, photos: list) -> list[dict]:
        """Enregistrements pour l'export et les statistiques (index + métadonnées + annotations)."""
        keys = [self.meta_key(p) for p in photos]
        metas = self.meta_records(keys)
        snap = self.annotation_source()
        fav = snap.favorites()
        tag_of: dict[str, list] = {}
        for t, ks in snap.tags.items():
            for k in ks:
                tag_of.setdefault(k, []).append(t)
        terms: dict[str, list] = {}
        if self.mode == MODE_LIBRARY and os.path.isfile(self.meta_path()):
            try:
                con = self.meta_index().connect()
                for r in con.execute("SELECT key, value FROM apple_terms WHERE kind = 'keyword'"):
                    terms.setdefault(r[0], []).append(r[1])
                con.close()
            except sqlite3.Error:
                pass
        out = []
        for p, mk in zip(photos, keys):
            ak = self._key(p)[1]
            m = metas.get(mk, {})
            rec = {k: m.get(k) for k in ("status", "width", "height", "has_exif", "make", "model", "lens", "focal",
                                         "focal35", "aperture", "exposure", "iso", "taken", "gps_lat", "gps_lon",
                                         "gps_alt")}
            rec.update(filename=p.filename, media_type=p.media_type, caption=p.caption, comment=p.comment,
                       rating=p.rating or None, event=p.event_name,
                       path=m.get("path") or (p.image_path if p.source == "folder" else self.library.to_linux(p.image_path)),
                       date=storage.apple_to_unix(p.date), file_size=m.get("file_size") or p.size or None,
                       favorite=ak in fav, tags=sorted(tag_of.get(ak, []), key=normalize_text),
                       keywords=sorted(terms.get(mk, [])))
            out.append(rec)
        return out

    # -- export -----------------------------------------------------------------------------
    def export_metadata(self, scope: str, fmt: str, fields: list, path: str) -> None:
        photos = self.scope_photos(scope)
        if not photos:
            messagebox.showinfo("Export", "Aucune photo à exporter.", parent=self.root)
            return
        records = self.build_records(photos)
        protected = self._protected_roots()

        def work(task):
            task.total = len(records)

            def prog(n):
                task.done = n
                task.text = f"{format_count(n)} / {format_count(len(records))} photos"
            return mexport.write(path, fmt, records, fields, protected, task.cancel, prog)

        def done(n, error):
            if isinstance(error, InterruptedError):
                self._set_status("Export annulé : aucun fichier écrit.")
            elif error is not None:
                messagebox.showerror("Export", f"Export impossible :\n{error}", parent=self.root)
            else:
                self._set_status(f"Export {fmt.upper()} : {plural(n, 'photo')} dans {safe_text(path)}")
                messagebox.showinfo("Export terminé", f"{plural(n, 'photo exportée', 'photos exportées')} dans :\n"
                                    f"{safe_text(path)}", parent=self.root)

        TaskDialog(self, self.root, "Export", f"Export de {plural(len(records), 'photo')} en {fmt.upper()}", work, done,
                   icon="file")

    # -- statistiques ------------------------------------------------------------------------
    def compute_stats(self, scope: str, done: Callable) -> None:
        photos = self.scope_photos(scope)
        cache_key = (self.mode, self.library.root, scope, len(photos),
                     os.path.getmtime(self.meta_path()) if os.path.isfile(self.meta_path()) else 0,
                     self.view.title if scope == "view" else "", tuple(sorted(self.selection)) if scope == "selection" else ())
        if self._stats_cache.get("key") == cache_key:
            done(self._stats_cache["value"], None)
            return
        records = self.build_records(photos)

        def work():
            return statlib.compute(records)

        def finished(result, error):
            if error is None:
                self._stats_cache = {"key": cache_key, "value": result}
            done(result, error)
        self._run_background(work, finished)

    def open_stats(self) -> None:
        if self.photos is None or self.total_photos() == 0:
            messagebox.showinfo("Statistiques", "Ouvrez d'abord une bibliothèque ou un dossier.", parent=self.root)
            return
        StatsWindow(self)

    # -- doublons -------------------------------------------------------------------------------
    def _dup_results_path(self) -> str:
        if self.mode == MODE_LIBRARY:
            return os.path.join(self.cache.cache_dir, "doublons.json")
        h = hashlib.sha1((self.library.root or "collections").encode("utf-8", "surrogateescape")).hexdigest()[:16]
        return os.path.join(FOLDER_CACHE_DIR, f"doublons-{h}.json")

    def find_duplicates(self) -> None:
        if self.photos is None or self.total_photos() == 0:
            messagebox.showinfo("Doublons", "Ouvrez d'abord une bibliothèque ou un dossier.", parent=self.root)
            return
        scope = "selection" if len(self.selection) >= 2 else "source"
        if scope == "source" and not self._view_is_whole_source() and self.total >= 2:
            ans = messagebox.askyesnocancel("Doublons", f"Analyser seulement la vue affichée ({plural(self.total, 'photo')}) ?"
                                            f"\n\nNon : toute la source ({plural(self.total_photos(), 'photo')}).",
                                            parent=self.root)
            if ans is None:
                return
            scope = "view" if ans else "source"
        photos = [p for p in self.scope_photos(scope) if not p.is_video]
        lib = self.library
        idx = self.meta_index()
        metas = self.meta_records([self.meta_key(p) for p in photos])
        entries = []
        for p in photos:
            mk = self.meta_key(p)
            m = metas.get(mk, {})
            entries.append(dupes.Entry(key=mk, path="", size=m.get("file_size") or p.size or 0, width=m.get("width"),
                                       height=m.get("height"), date=storage.apple_to_unix(p.date),
                                       label=p.filename))
        cached = {}
        if os.path.isfile(self.meta_path()):
            try:
                con = idx.connect()
                cached = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
                    "SELECT key, sha256, phash, hash_sig FROM photo_meta WHERE sha256 IS NOT NULL")}
                con.close()
            except sqlite3.Error:
                cached = {}

        def work(task):
            task.total = len(entries)
            task.text = "Recherche des fichiers…"
            pending = []
            for e, p in zip(entries, photos):
                if task.cancel.is_set():
                    return None
                e.path = resolve_photo_file(lib, p, True) or ""
                sig = storage.file_signature(e.path) if e.path else None
                c = cached.get(e.key)
                if c and sig and c[2] == sig:
                    e.sha256, e.phash = c[0], c[1]
                if not e.size and sig:
                    e.size = int(sig.split(":")[0])
                if e.path and not e.width:      # dimensions lues dans l'en-tête (lecture seule, rapide)
                    try:
                        with Image.open(e.path) as im:
                            e.width, e.height = im.size
                    except Exception:
                        pass
            usable = [e for e in entries if e.path]

            def prog(d, n):
                task.done = d
                task.text = f"Empreintes : {format_count(d)} / {format_count(n)} fichiers (lecture seule)"

            def hashed(e):
                pending.append((e.key, storage.file_signature(e.path), e.sha256, e.phash))
                if len(pending) >= 200:
                    try:
                        idx.set_hashes(pending[:])
                    except sqlite3.Error:
                        pass
                    pending.clear()
            bad = dupes.hash_entries(usable, True, prog, task.cancel, hashed)
            if pending:
                try:
                    idx.set_hashes(pending)
                except sqlite3.Error:
                    pass
            if task.cancel.is_set():
                return None
            task.text = "Regroupement…"
            rep = dupes.find_groups(usable)
            rep.unreadable = bad + (len(entries) - len(usable))
            return rep

        def done(rep, error):
            if error is not None:
                messagebox.showerror("Doublons", f"Analyse impossible :\n{error}", parent=self.root)
                return
            if rep is None:
                self._set_status("Recherche de doublons annulée (les empreintes calculées sont conservées).")
                return
            try:
                path = self._dup_results_path()
                storage.assert_outside(path, self._protected_roots())
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump({"time": time.time(), "analysed": rep.analysed,
                               "groups": [g.to_json() for g in rep.groups]}, fh)
                os.replace(path + ".tmp", path)
            except (OSError, PermissionError):
                pass
            c = rep.counts()
            self._set_status(f"Doublons : {c[dupes.IDENTICAL]} groupe(s) identiques, {c[dupes.NEAR]} quasi-identiques, "
                             f"{c[dupes.SIMILAR]} proches sur {format_count(rep.analysed)} fichiers.")
            DuplicatesWindow(self, rep)

        TaskDialog(self, self.root, "Doublons", f"Recherche des doublons parmi {plural(len(entries), 'photo')}",
                   work, done, icon="copy")

    def _view_is_whole_source(self) -> bool:
        if self.view.kind in ("empty", "timeline", "all"):
            return True
        if self.mode == MODE_FOLDER and self.view.kind == "folder":
            f = self.folder
            return not (f.subdir or f.only is not None or f.query or f.date_range is not None)
        return False

    def open_last_duplicates(self) -> None:
        path = self._dup_results_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            messagebox.showinfo("Doublons", "Aucun résultat enregistré pour cette source : lancez d'abord une recherche.",
                                parent=self.root)
            return
        rep = dupes.Report([dupes.Group.from_json(g) for g in data.get("groups", [])], data.get("analysed", 0))
        DuplicatesWindow(self, rep, saved_at=data.get("time"))

    # -- comparaison, carte, diaporama ------------------------------------------------------
    def compare_selection(self) -> None:
        photos = self.selected_photos()
        if len(photos) != 2:
            messagebox.showinfo("Comparer", "Sélectionnez exactement deux photos (Ctrl+clic).", parent=self.root)
            return
        items = []
        for p in photos:
            path, _o = self.library.find_display_file(p, for_thumbnail=False)
            if not path:
                messagebox.showwarning("Comparer", f"Fichier introuvable : {p.filename}", parent=self.root)
                return
            items.append((p.label, path, self.meta_key(p)))
        CompareWindow(self, items)

    def open_map(self) -> None:
        if self.photos is None or self.mode == MODE_COLLECTIONS:
            messagebox.showinfo("Carte", "Ouvrez une bibliothèque ou un dossier.", parent=self.root)
            return
        total, analysed, _ex = self.metadata_coverage()
        if analysed == 0:
            if messagebox.askyesno("Carte", "Les coordonnées GPS viennent de l'analyse des métadonnées, qui n'a pas "
                                   "encore été faite pour cette source.\n\nLancer l'analyse maintenant ?",
                                   parent=self.root):
                self.analyze_metadata(done=self.open_map)
            return
        points = []
        try:
            con = self.meta_index().connect()
            places = {}
            for r in con.execute("SELECT key, value FROM apple_terms WHERE kind = 'place'"):
                places.setdefault(r[0], r[1])
            rows = con.execute("SELECT key, photo_id, gps_lat, gps_lon, path FROM photo_meta "
                               "WHERE gps_lat IS NOT NULL").fetchall()
            con.close()
        except sqlite3.Error as exc:
            messagebox.showerror("Carte", str(exc), parent=self.root)
            return
        allowed = None if self.mode == MODE_LIBRARY else {path_key(p.image_path) for p in self.folder.photos}
        for r in rows:
            if allowed is not None and r[0] not in allowed:
                continue
            points.append(geo.Point(r[0], r[1], r[2], r[3], os.path.basename(r[4] or ""), places.get(r[0], "")))
        GeoWindow(self, points, total - analysed)

    def start_slideshow(self, scope: str = "") -> None:
        if not scope:
            scope = "selection" if len(self.selection) > 1 else "view"
        photos = self.scope_photos(scope)
        if not photos:
            messagebox.showinfo("Diaporama", "Aucune photo à afficher : ouvrez un album, un dossier ou sélectionnez "
                                "des photos.", parent=self.root)
            return
        start = 0
        if scope == "view" and self.hover_index is not None:
            start = self.page_no * PAGE_SIZE + self.hover_index
        SlideshowWindow(self, photos, start)

    # -- mots-clés Apple ------------------------------------------------------------------------
    def import_apple_terms(self) -> None:
        if self.mode != MODE_LIBRARY or self.photos is None:
            messagebox.showinfo("Informations Apple", "Disponible pour une bibliothèque iPhoto ou Photos ouverte.",
                                parent=self.root)
            return
        if not self.library.is_available():
            messagebox.showwarning("Informations Apple", "Bibliothèque inaccessible (disque débranché ?).",
                                   parent=self.root)
            return
        root, workdir, protected = self.library.root, self.cache.cache_dir, self._protected_roots()
        idx = self.meta_index()

        def work(task):
            task.text = "Lecture des bases Apple (copiées dans le cache, jamais ouvertes sur place)…"
            res = applemeta.import_terms(root, workdir, protected)
            idx.replace_terms("keyword", res.keywords)
            idx.replace_terms("person", res.persons)
            idx.replace_terms("place", res.places)
            return res

        def done(res, error):
            if error is not None:
                messagebox.showerror("Informations Apple", f"Lecture impossible :\n{error}", parent=self.root)
                return
            self._attach_meta()
            rows = [("#", "Résultat"), ("Importé", res.summary()),
                    ("Bases lues", ", ".join(res.sources) or "aucune")]
            if res.notes:
                rows += [("#", "Remarques")] + [("•", n) for n in res.notes]
            rows += [("#", "Utilisation"), ("Recherche avancée", "critères « Mot-clé Apple », « Personne », « Lieu »"),
                     ("Visionneuse", "affichés sous la photo"),
                     ("Fiabilité", "lecture expérimentale : schéma détecté, non vérifié sur toutes les versions")]
            InfoDialog(self, self.root, "Informations Apple", "Mots-clés, personnes et lieux", res.summary(), rows,
                       icon="tag")
            self._set_status("Informations Apple : " + res.summary())

        TaskDialog(self, self.root, "Informations Apple", "Import des mots-clés, personnes et lieux", work, done,
                   icon="tag", cancellable=False)

    # -- dossiers récents, favoris, surveillance ------------------------------------------------
    def _rebuild_folder_menus(self) -> None:
        for menu, lst, empty in ((self.m_recent_folders, self.settings.recent_folders, "(aucun dossier récent)"),
                                 (self.m_fav_folders, self.settings.folder_favorites, "(aucun dossier favori)")):
            menu.delete(0, "end")
            if menu is self.m_fav_folders:
                menu.add_command(label="Ajouter le dossier ouvert aux favoris", command=self.add_folder_favorite)
                menu.add_command(label="Retirer le dossier ouvert des favoris", command=self.remove_folder_favorite)
                menu.add_separator()
            if not lst:
                menu.add_command(label=empty, state="disabled")
            for path in lst:
                label = os.path.basename(path.rstrip("/")) or path
                menu.add_command(label=f"{safe_text(label)}   —   {safe_text(os.path.dirname(path))}",
                                 command=lambda p=path: self.open_folder_path(p))

    def open_folder_path(self, path: str) -> None:
        if not os.path.isdir(path):
            messagebox.showerror("Dossier inaccessible", f"Ce dossier n'existe pas ou n'est plus accessible :\n"
                                 f"{safe_text(path)}\n\nLe disque est peut-être débranché.", parent=self.root)
            return
        if self.mode != MODE_FOLDER:
            self.set_mode(MODE_FOLDER, restore=False)
            if self.mode != MODE_FOLDER:
                return
        self.open_folder(path)

    def add_folder_favorite(self) -> None:
        root = self.folder.root
        if not root:
            messagebox.showinfo("Dossiers favoris", "Ouvrez d'abord un dossier.", parent=self.root)
            return
        if root not in self.settings.folder_favorites:
            self.settings.folder_favorites.append(root)
            self.settings.save()
        self._rebuild_folder_menus()
        self._set_status(f"« {safe_text(os.path.basename(root))} » ajouté aux dossiers favoris.")

    def remove_folder_favorite(self) -> None:
        root = self.folder.root
        if root in self.settings.folder_favorites:
            self.settings.folder_favorites.remove(root)
            self.settings.save()
            self._rebuild_folder_menus()

    def _folder_snapshot(self, scanner: "FolderScanner") -> str:
        """Compare avec la dernière visite (instantané dans le cache) ; renvoie un résumé."""
        try:
            new = folderlib.entries_signature(scanner.entries)
            old, when, rec = folderlib.load_snapshot(FOLDER_CACHE_DIR, scanner.root)
            folderlib.save_snapshot(FOLDER_CACHE_DIR, scanner.root, new, scanner.recursive)
            self._folder_sig = new
        except (OSError, PermissionError, ValueError):
            return ""
        if old is None or rec != scanner.recursive:
            return ""
        d = folderlib.compare(old, new)
        if d.empty:
            return ""
        return f"Depuis la dernière visite : {d.summary()}"

    def toggle_folder_watch(self) -> None:
        self.settings.folder_watch = bool(self.watch_var.get())
        self.settings.save()
        self._set_status("Surveillance du dossier activée (vérification toutes les 30 s, sans rien modifier)."
                         if self.settings.folder_watch else "Surveillance du dossier désactivée.")

    def _watch_tick(self) -> None:
        """Toutes les 5 s : disque de la source ; toutes les 30 s : dossier surveillé (passif)."""
        try:
            self._watch_disk()
            self._watch_count += 1
            if (self._watch_count % 6 == 0 and self.settings.folder_watch and self.mode == MODE_FOLDER
                    and self.folder.root and self._scanner is None and not self._watch_busy):
                self._watch_folder()
        finally:
            self.root.after(5000, self._watch_tick)

    def _watch_folder(self) -> None:
        root, recursive, old = self.folder.root, self.folder.recursive, dict(self._folder_sig or {})
        self._watch_busy = True

        def work():
            sc = FolderScanner(root, recursive)
            sc.run()
            return folderlib.compare(old, folderlib.entries_signature(sc.entries))

        def done(d, error):
            self._watch_busy = False
            if error is None and d is not None and not d.empty and self.folder.root == root:
                self._set_status(f"Le dossier a changé : {d.summary()} — appuyez sur F5 pour actualiser.")
        self._run_background(work, done)

    def _watch_disk(self) -> None:
        if self.mode != MODE_LIBRARY or self._building or not self.library.root:
            return
        avail = self.library.is_available()
        if self._disk_ok is None:
            self._disk_ok = avail
            return
        if avail == self._disk_ok:
            return
        self._disk_ok = avail
        if not avail:
            self.loader.cancel_pending()
            self._set_status("⚠ Bibliothèque déconnectée : l'index reste consultable, les originaux ne sont plus "
                             "accessibles. Rebranchez le disque : la reprise est automatique.")
        else:
            self._set_status("Bibliothèque de nouveau accessible : les fichiers sont relus.")
            if self.page_photos and self.view.kind != "timeline":
                self.load_page()

    # -- maintenance et diagnostic -------------------------------------------------------------
    def show_diagnostic(self) -> None:
        rows = [("#", "Dépendances")]
        for d in depcheck.check():
            state = "présent" if d.available else ("ABSENT" if d.required else "absent (facultatif)")
            rows.append((d.name, f"{state} — {d.role} ({d.detail})"
                         + ("" if d.available else f" — paquet : {d.package}"), "" if d.available or not d.required else "warn"))

        def size_of(path):
            total = 0
            for dp, _dn, fn in os.walk(path):
                for f in fn:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
            return total
        rows += [("#", "Stockage local (jamais dans les sources)"),
                 ("Cache", f"{CACHE_DIR} — {format_size(size_of(CACHE_DIR))}"),
                 ("Préférences", CONFIG_PATH), ("Favoris et tags", ANNOTATIONS_PATH),
                 ("Albums virtuels", self.collections.path)]
        rows.append(("#", "Source courante"))
        if self.mode == MODE_LIBRARY:
            meta = self.cache.read_meta()
            rows += [("Bibliothèque", self.library.root), ("Type", self.library.kind or "inconnu"),
                     ("Accessible", "oui" if self.library.is_available() else "non (disque débranché ?)",
                      "" if self.library.is_available() else "warn"),
                     ("Index", f"{meta.get('photo_count', '?')} photos, construit le "
                               f"{meta.get('built_at', '—').replace('T', ' à ')}")]
        else:
            rows += [("Dossier", self.library.root or "—")]
        total, analysed, with_exif = self.metadata_coverage()
        rows.append(("Métadonnées analysées", f"{format_count(analysed)} / {format_count(total)} (dont "
                                               f"{format_count(with_exif)} avec EXIF)"))
        try:
            missing = self.collections.missing_refs()
            albums = self.collections.albums()
            rows += [("#", "Collections"), ("Albums virtuels", format_count(sum(1 for a in albums if a.kind == "manual"))),
                     ("Albums intelligents", format_count(sum(1 for a in albums if a.kind == "smart"))),
                     ("Recherches enregistrées", format_count(len(self.collections.searches()))),
                     ("Références introuvables", format_count(len(missing)), "warn" if missing else "")]
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            rows.append(("Collections", f"illisibles : {exc}", "warn"))
        InfoDialog(self, self.root, "Diagnostic", "Diagnostic d'iphotor", f"iphotor {APP_VERSION}", rows, icon="info")

    def clean_obsolete_caches(self) -> None:
        """Supprime les caches de bibliothèques qui n'existent plus et ne sont pas récentes (cache seulement)."""
        keep = {os.path.abspath(p) for p in self.settings.recent} | {self.library.root}
        victims = []
        try:
            names = os.listdir(CACHE_DIR)
        except OSError:
            names = []
        for name in names:
            d = os.path.join(CACHE_DIR, name)
            idx = os.path.join(d, "index.sqlite3")
            if not os.path.isfile(idx):
                continue
            try:
                c = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
                root = dict(c.execute("SELECT key, value FROM meta").fetchall()).get("library_root", "")
                c.close()
            except sqlite3.Error:
                root = ""
            if root and root not in keep and not os.path.exists(root):
                victims.append((d, root))
        if not victims:
            messagebox.showinfo("Caches obsolètes", "Aucun cache obsolète : chaque cache correspond à une bibliothèque "
                                "récente ou encore présente.", parent=self.root)
            return
        if not messagebox.askyesno("Caches obsolètes", "Supprimer les caches (index et miniatures) de ces "
                                   "bibliothèques introuvables ?\n\n" + "\n".join(r for _d, r in victims[:15])
                                   + "\n\nLes bibliothèques elles-mêmes ne sont pas concernées.", parent=self.root):
            return
        for d, _r in victims:
            if storage.is_inside(d, CACHE_DIR) and os.path.realpath(d) != os.path.realpath(CACHE_DIR):
                shutil.rmtree(d, ignore_errors=True)
        self._set_status(f"{plural(len(victims), 'cache obsolète supprimé', 'caches obsolètes supprimés')}.")

    # -- menus -------------------------------------------------------------------
    def clear_thumb_cache(self) -> None:
        what = ("Les dossiers consultés ne sont pas concernés." if self.mode == MODE_FOLDER
                else "La bibliothèque iPhoto n'est pas concernée.")
        if messagebox.askyesno("Vider le cache",
                               "Supprimer les miniatures enregistrées dans le cache local ?\n"
                               f"({self.cache.thumb_dir})\n\n{what}",
                               parent=self.root):
            self.loader.cancel_pending()
            try:
                self.cache.clear_thumbnails()
            except (PermissionError, OSError) as exc:
                messagebox.showerror("Cache", f"Impossible de vider le cache :\n{exc}", parent=self.root)
                return
            self.load_page()
            self._set_status("Cache des miniatures vidé.")

    def show_cache_info(self) -> None:
        count, size = self.cache.thumbnails_size()
        if self.mode == MODE_FOLDER:
            messagebox.showinfo(
                "Cache local",
                f"Dossier du cache : {self.cache.cache_dir}\n\n"
                f"Miniatures en cache : {format_count(count)} ({size / 1e6:.1f} Mo)\n\n"
                f"Dossier consulté : {safe_text(self.library.root) or '—'}\n"
                f"Images trouvées : {format_count(len(self.folder.photos))}\n"
                "Le dossier consulté n'est jamais modifié.",
                parent=self.root)
            return
        meta = self.cache.read_meta()
        idx_size = os.path.getsize(self.cache.index_path) if os.path.isfile(self.cache.index_path) else 0
        messagebox.showinfo(
            "Cache local",
            f"Dossier : {self.cache.cache_dir}\n\n"
            f"Index : {idx_size / 1e6:.1f} Mo (construit le {meta.get('built_at', '—').replace('T', ' à ')})\n"
            f"Photos indexées : {format_count(int(meta.get('photo_count', 0) or 0))}\n"
            f"Miniatures en cache : {format_count(count)} ({size / 1e6:.1f} Mo)\n\n"
            f"Bibliothèque : {self.library.root}\n"
            f"Accessible : {'oui' if self.library.is_available() else 'non'}",
            parent=self.root)

    def show_shortcuts(self) -> None:
        messagebox.showinfo(
            "Raccourcis clavier",
            "Fenêtre principale :\n"
            "  Ctrl+O : ouvrir une autre bibliothèque\n  Ctrl+Maj+O : ouvrir un dossier\n"
            "  Ctrl+F : rechercher\n  F5 : recharger\n  Page préc. / Page suiv. : changer de page\n"
            "  Ctrl+Maj+S : copier l'album, l'événement ou le dossier sélectionné\n  Ctrl+Q : quitter\n\n"
            "Sur la vignette survolée (ou dans la visionneuse) :\n"
            "  Ctrl+C : copier l'image dans le presse-papiers\n  Ctrl+Maj+C : copier le chemin du fichier\n"
            "  Ctrl+S : copier vers un dossier\n  F : ajouter aux favoris / retirer\n  T : tags\n"
            "  I : informations EXIF\n  Clic droit : menu de la photo\n\n"
            "Visionneuse :\n"
            "  ← / → (ou Espace) : photo précédente / suivante\n  Début / Fin : première / dernière photo\n"
            "  Échap : fermer",
            parent=self.root)

    def show_about(self) -> None:
        messagebox.showinfo(
            "À propos",
            f"iphotor {APP_VERSION}\n\nExplorateur en lecture seule de bibliothèques iPhoto / Photos\n"
            "et de dossiers d'images.\n"
            "Les bibliothèques et les dossiers consultés ne sont jamais modifiés : l'index et les\n"
            f"miniatures sont stockés dans\n{CACHE_DIR}\n\n"
            f"Favoris et tags :\n{ANNOTATIONS_PATH}",
            parent=self.root)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def quit(self) -> None:
        if self._scanner is not None:
            self._scanner.cancel()
        for st in self._mode_states.values():
            if st.get("loader") is not None and st["loader"] is not self.loader:
                st["loader"].shutdown()
        self.loader.shutdown()
        self._close_db()
        self.annotations.close()
        self.collections.close()
        try:   # annule les rappels after() en attente (pas d'erreur Tcl après la fermeture)
            for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
                self.root.tk.call("after", "cancel", job)
        except tk.TclError:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Vérification de cohérence (ligne de commande)
# ---------------------------------------------------------------------------

def verify(library: LibraryManager, cache: CacheManager, sample: int = 300) -> int:
    """Contrôle les relations albums / dossiers / événements / photos de l'index."""
    import random
    con = cache.connect()
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    library.set_mac_root(meta.get("archive_path", ""))
    am = AlbumManager(con)
    pm = PhotoManager(con)
    problems = 0
    print(f"Index : {cache.index_path} (construit le {meta.get('built_at')})")
    print(f"Photos : {meta.get('photo_count')}  Albums (toutes catégories) : {meta.get('album_count')}  "
          f"Rolls : {meta.get('roll_count')}")

    kinds: dict[str, int] = {}
    for a in am.all():
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    print("Répartition :", ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    # 1. Parents : doivent exister et être des dossiers
    bad_parent = [a for a in am.all() if a.parent_id is not None and
                  (am.get(a.parent_id) is None or am.get(a.parent_id).kind != KIND_FOLDER)]
    print(f"[1] Albums dont le parent n'est pas un dossier existant : {len(bad_parent)}")
    problems += len(bad_parent)

    # Parents référencés dans le XML mais absents (liens coupés à l'indexation)
    raw_orphans = con.execute(
        "SELECT COUNT(*) FROM albums WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM albums)"
    ).fetchone()[0]
    print(f"[2] Références Parent orphelines : {raw_orphans}")
    problems += raw_orphans

    # 3. Homonymes : chacun doit garder son identifiant et son contenu propre
    by_name: dict[tuple[str, str], list[Album]] = {}
    for a in am.all():
        if a.kind in (KIND_ALBUM, KIND_FOLDER, KIND_SMART, KIND_EVENT):
            by_name.setdefault((a.kind, a.name), []).append(a)
    dups = {k: v for k, v in by_name.items() if len(v) > 1}
    print(f"[3] Noms partagés par plusieurs albums/dossiers/événements : {len(dups)} noms, "
          f"{sum(len(v) for v in dups.values())} entrées distinctes (identifiées par AlbumId)")
    shown = 0
    for (kind, name), items in sorted(dups.items(), key=lambda kv: -len(kv[1])):
        if shown >= 8:
            break
        shown += 1
        print(f"     « {name} » ({kind}) :")
        for a in items:
            n = con.execute("SELECT COUNT(*) FROM album_photos WHERE album_id=?", (a.id,)).fetchone()[0]
            print(f"        AlbumId {a.id:>6}  {n:>5} photos  dans : {am.path_label(a) or '(racine)'}")
            if n != a.photo_count:
                problems += 1
                print("        !! nombre de photos incohérent")

    # 4. Contenu des dossiers : les photos d'un dossier doivent provenir de ses sous-albums
    folder_issues = 0
    for f in (a for a in am.all() if a.kind == KIND_FOLDER):
        desc = []
        stack = [f.id]
        while stack:
            cur = stack.pop()
            for ch in am.children(cur):
                desc.append(ch.id)
                if ch.kind == KIND_FOLDER:
                    stack.append(ch.id)
        if not desc:
            continue
        q = ("SELECT COUNT(*) FROM album_photos WHERE album_id=? AND photo_id NOT IN "
             f"(SELECT photo_id FROM album_photos WHERE album_id IN ({','.join('?' * len(desc))}))")
        extra = con.execute(q, [f.id] + desc).fetchone()[0]
        if extra:
            folder_issues += 1
    print(f"[4] Dossiers contenant des photos absentes de leurs sous-albums : {folder_issues}")

    # 5. Événements <-> Rolls <-> photos
    ev_no_roll = con.execute("SELECT COUNT(*) FROM albums WHERE kind='event' AND roll_id IS NULL").fetchone()[0]
    mismatch = con.execute(
        "SELECT COUNT(*) FROM album_photos ap JOIN albums e ON e.id=ap.album_id AND e.kind='event' "
        "JOIN photos p ON p.id=ap.photo_id WHERE p.roll_id != e.roll_id").fetchone()[0]
    photos_no_event = con.execute(
        "SELECT COUNT(*) FROM photos p WHERE NOT EXISTS (SELECT 1 FROM albums e WHERE e.kind='event' AND e.roll_id=p.roll_id)"
    ).fetchone()[0]
    print(f"[5] Événements sans Roll correspondant : {ev_no_roll}")
    print(f"    Photos d'un événement rattachées à un autre Roll : {mismatch}")
    print(f"    Photos sans événement : {photos_no_event}")
    problems += ev_no_roll + mismatch

    # 6. Références de photos inexistantes (ignorées à l'indexation)
    print(f"[6] Références KeyList vers des photos inexistantes (ignorées) : {meta.get('stale_keys')}")
    dangling = con.execute(
        "SELECT COUNT(*) FROM album_photos WHERE photo_id NOT IN (SELECT id FROM photos)").fetchone()[0]
    print(f"    Liens album→photo invalides dans l'index : {dangling}")
    problems += dangling

    # 7. Fichiers sur le disque (échantillon)
    if library.is_available():
        ids = [r[0] for r in con.execute("SELECT id FROM photos")]
        random.seed(1)
        pick = random.sample(ids, min(sample, len(ids)))
        ok_orig = ok_disp = 0
        for pid in pick:
            p = pm.get(pid)
            ok_orig += library.original_exists(p)
            path, _o = library.find_display_file(p, for_thumbnail=True)
            ok_disp += path is not None
        print(f"[7] Échantillon de {len(pick)} photos : original présent {ok_orig}, "
              f"aperçu affichable {ok_disp}")
        # Chemins avec caractères accentués (normalisation Unicode HFS+)
        acc = [r[0] for r in con.execute("SELECT id FROM photos WHERE image_path GLOB '*[^ -~]*' LIMIT 200")]
        ok_acc = sum(library.original_exists(pm.get(pid)) for pid in acc)
        print(f"    Chemins non ASCII testés : {len(acc)}, trouvés : {ok_acc}")
    else:
        print("[7] Bibliothèque inaccessible : vérification des fichiers ignorée.")

    con.close()
    print()
    print("Résultat :", "cohérent ✔" if problems == 0 else f"{problems} anomalie(s) détectée(s)")
    return 0 if problems == 0 else 1


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Explorateur en lecture seule d'une bibliothèque iPhoto.")
    ap.add_argument("--bibliotheque", "-b", default=None,
                    help="chemin de la bibliothèque iPhoto (défaut : la dernière ouverte, "
                         f"sinon {DEFAULT_LIBRARY})")
    ap.add_argument("--reindexer", action="store_true", help="reconstruire l'index local")
    ap.add_argument("--diagnostic", action="store_true",
                    help="dépendances facultatives et emplacements des données (sans interface graphique)")
    ap.add_argument("--verifier", action="store_true",
                    help="vérifier la cohérence de l'index (sans interface graphique)")
    args = ap.parse_args()

    migrate_legacy_cache()
    settings = Settings()
    root_path = (args.bibliotheque or os.environ.get("IPHOTOR_LIBRARY")
                 or settings.last_library or DEFAULT_LIBRARY)
    library = LibraryManager(root_path)
    try:
        cache = CacheManager(library)
        cache.ensure_dirs()
    except (PermissionError, OSError) as exc:
        print(f"Erreur : impossible de préparer le cache local : {exc}", file=sys.stderr)
        return 2

    if args.diagnostic:
        print(f"iphotor {APP_VERSION} — diagnostic\n")
        for line in depcheck.report_lines():
            print("  " + line)
        print(f"\n  Cache            : {CACHE_DIR}\n  Préférences      : {CONFIG_PATH}\n"
              f"  Favoris et tags  : {ANNOTATIONS_PATH}\n"
              f"  Albums virtuels  : {os.path.join(storage.data_root(), vcoll.DB_NAME)}")
        col_path = os.path.join(storage.data_root(), vcoll.DB_NAME)
        if os.path.isfile(col_path):
            try:
                col = vcoll.Collections(col_path)
                print(f"  Albums virtuels : {len(col.albums('manual'))}, intelligents : {len(col.albums('smart'))}, "
                      f"références introuvables : {len(col.missing_refs())}")
                col.close()
            except Exception as exc:
                print(f"  Collections illisibles : {exc}")
        else:
            print("  Aucun album virtuel créé pour l'instant.")
        return 0

    if args.verifier or (args.reindexer and tk is None):
        if args.reindexer or not cache.is_valid():
            if not library.is_available():
                if not cache.index_exists():
                    print(library.unavailable_message(), file=sys.stderr)
                    return 2
                print("Bibliothèque inaccessible : utilisation de l'index existant.")
            else:
                stats = cache.build(progress=print)
                print(stats)
        if args.verifier:
            return verify(library, cache)
        return 0

    if tk is None or Image is None or ImageTk is None:
        missing = []
        if tk is None:
            missing.append("python3-tk")
        if Image is None:
            missing.append("python3-pil")
        if ImageTk is None:
            missing.append("python3-pil.imagetk")
        print("Dépendances manquantes : " + ", ".join(missing) + "\n"
              "Installez-les avec :\n  sudo apt install " + " ".join(missing), file=sys.stderr)
        return 3

    root = tk.Tk(className="iphotor")
    MainWindow(root, library, cache, settings=settings, force_reindex=args.reindexer)

    def report_callback_exception(exc, val, tb):
        traceback.print_exception(exc, val, tb)
        try:
            messagebox.showerror("Erreur inattendue",
                                 f"Une erreur inattendue s'est produite :\n{val}\n\n"
                                 "L'application continue de fonctionner.", parent=root)
        except Exception:
            pass

    root.report_callback_exception = report_callback_exception
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
