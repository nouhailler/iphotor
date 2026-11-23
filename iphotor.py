#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iphotor — Explorateur en lecture seule d'une ancienne bibliothèque iPhoto ou Photos (1 à 4),
et de n'importe quel dossier d'images (mode « Explorer un dossier »).

Version 1.4

Principe fondamental : la bibliothèque iPhoto et les dossiers consultés ne sont JAMAIS modifiés.
  * Tous les fichiers de la bibliothèque sont ouverts en lecture seule ("rb").
  * Aucun fichier n'est créé, déplacé, renommé ou supprimé dans la bibliothèque.
  * L'index et le cache des miniatures sont stockés dans
    ~/.cache/photoexplorer_iphoto/ (ou $XDG_CACHE_HOME/photoexplorer_iphoto/).
  * Les miniatures des dossiers explorés vont dans ~/.cache/photoexplorer_iphoto/dossiers/.
  * Les favoris et les tags sont enregistrés dans ~/.local/share/iphotor/annotations.sqlite3.
  * Les copies (« Copier vers un dossier… ») ne lisent que les fichiers sources ; elles sont
    refusées vers une bibliothèque ou un dossier consulté et ne remplacent jamais un fichier.

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
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 400_000_000  # grands scans tolérés
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

try:
    from PIL import ImageTk
except ImportError:  # pragma: no cover
    ImageTk = None  # type: ignore


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME = "iphotor"
APP_VERSION = "1.4.0"

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
TILE_H = 214
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

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"last_library": self.last_library, "recent": self.recent,
                           "last_folder": self.last_folder, "folder_recursive": self.folder_recursive,
                           "folder_sort": self.folder_sort, "copy_dest": self.copy_dest,
                           "copy_original": self.copy_original, "copy_open": self.copy_open},
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


def load_pil_image(path: str, max_w: int, max_h: int):
    """Ouvre une image en lecture seule et la réduit (sans déformation).

    Gère aussi le HEIC (via GdkPixbuf) et les RAW TIFF (aperçu JPEG intégré).
    """
    ext = file_ext(path)
    if ext in HEIF_EXTS:
        if not heif_supported():
            raise ValueError("format HEIC non pris en charge (paquet heif-gdk-pixbuf absent)")
        img = _load_heif(path, max_w, max_h)
    else:
        raw_orientation = None
        if ext in RAW_TIFF_EXTS:
            data, raw_orientation = extract_raw_preview(path)
            if data is None:
                raise ValueError("aucun aperçu JPEG intégré dans ce fichier RAW")
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

FOLDER_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}
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

COLORS = {
    "favorite": "#ff5c7a",
    "bg": "#1f2124",
    "panel": "#2a2d31",
    "tile": "#2f3237",
    "tile_hover": "#3b4047",
    "text": "#e8e8e8",
    "muted": "#9aa0a6",
    "accent": "#4a9eff",
    "missing": "#e5534b",
    "video": "#f0a030",
    "placeholder": "#3a3d42",
}


class PhotoViewer:
    """Fenêtre de visualisation d'une photo, avec navigation."""

    def __init__(self, app: "MainWindow"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Visualisation — iphotor")
        self.win.configure(bg=COLORS["bg"])
        sw, sh = app.root.winfo_screenwidth(), app.root.winfo_screenheight()
        self.max_w, self.max_h = min(sw, 3000), min(sh, 2000)
        self.win.geometry(f"{int(sw * 0.75)}x{int(sh * 0.8)}")
        self.win.minsize(500, 400)
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

        # Barre de navigation
        bar = ttk.Frame(self.win, style="Panel.TFrame", padding=(8, 6))
        bar.pack(side="top", fill="x")
        self.btn_prev = ttk.Button(bar, text="◀  Précédente", command=self.prev)
        self.btn_prev.pack(side="left")
        self.btn_next = ttk.Button(bar, text="Suivante  ▶", command=self.next)
        self.btn_next.pack(side="left", padx=(6, 0))
        self.pos_label = ttk.Label(bar, text="", style="Panel.TLabel")
        self.pos_label.pack(side="left", padx=12)
        self.btn_open = ttk.Button(bar, text="Ouvrir ↗", command=self.open_external)
        self.btn_open.pack(side="right")
        ttk.Button(bar, text="Copier vers…", command=lambda: self._act("to")).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Copier", command=lambda: self._act("copy")).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="EXIF", command=lambda: self._act("exif")).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="🏷 Tags", command=lambda: self._act("tags")).pack(side="right", padx=(0, 6))
        self.btn_fav = ttk.Button(bar, text="♡ Favori", width=11, command=lambda: self._act("fav"))
        self.btn_fav.pack(side="right", padx=(0, 6))

        # Informations
        info = ttk.Frame(self.win, style="Panel.TFrame", padding=(12, 8))
        info.pack(side="bottom", fill="x")
        self.info_title = ttk.Label(info, text="", style="Title.TLabel")
        self.info_title.pack(anchor="w")
        self.info_details = ttk.Label(info, text="", style="Panel.TLabel", justify="left")
        self.info_details.pack(anchor="w", pady=(2, 0))
        self.info_comment = ttk.Label(info, text="", style="Muted.TLabel", justify="left")
        self.info_comment.pack(anchor="w", pady=(2, 0))
        self.info_annot = ttk.Label(info, text="", style="Favorite.TLabel", justify="left")
        self.info_annot.pack(anchor="w", pady=(2, 0))

        # Image
        self.canvas = tk.Canvas(self.win, bg="#111214", highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

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
        self._poll()

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
        if p is None:
            self.info_annot.config(text="")
            return
        fav = self.app.is_favorite(p)
        self.btn_fav.config(text="♥ Favori" if fav else "♡ Favori")
        parts = ["♥ Dans vos favoris"] if fav else []
        tags = self.app.tags_of(p)
        if tags:
            parts.append("🏷 Tags : " + ", ".join(tags))
        self.info_annot.config(text="     ".join(parts))

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
        if p.source == "folder":
            details = [f"Dossier : {p.folder or '.'}"]
            if p.date is not None:
                details.append(f"Modifiée le : {format_date(p.date)}")
            details.append(f"Taille : {format_size(p.size)}")
            self._details = "     ".join(details)
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
        self._details = "     ".join(details)
        self.info_details.config(text=self._details)
        comment = p.comment if p.comment and p.comment != p.caption else ""
        self.info_comment.config(text=f"Commentaire : {comment}" if comment else "")

    def _message(self, text: str, color: str = COLORS["muted"]) -> None:
        self.canvas.delete("all")
        w, h = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        self.canvas.create_text(w // 2, h // 2, text=text, fill=color, font=self.app.font_big,
                                justify="center", width=w - 40)

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
                        text=f"{self._details}     Dimensions : {dims[0]} × {dims[1]} px")
                if img is None:
                    self._message(err, COLORS["missing"])
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
        iw, ih = self.pil.size
        scale = min(cw / iw, ch / ih, 1.0)
        size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
        img = self.pil if size == self.pil.size else self.pil.resize(size, Image.BILINEAR)
        self.tkimg = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self.tkimg, anchor="center")
        note = getattr(self, "note", "")
        if note:
            self.canvas.create_text(10, ch - 10, text=note, fill=COLORS["video"] if self.photo and self.photo.is_video else COLORS["muted"],
                                    anchor="sw", font=self.app.font_small)

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
        self.win.title("Ouvrir une bibliothèque")
        self.win.configure(bg=COLORS["panel"])
        self.win.transient(app.root)
        self.win.geometry("860x460")
        self.win.minsize(600, 320)
        self.results: "queue.Queue[list]" = queue.Queue()
        self.cancel = threading.Event()
        self.paths: dict[str, tuple[str, str]] = {}   # iid -> (chemin, type)

        head = ttk.Frame(self.win, padding=(12, 10, 12, 4))
        head.pack(side="top", fill="x")
        ttk.Label(head, text="Choisissez la bibliothèque à explorer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text=f"Bibliothèque actuelle : {app.library.root}", style="Muted.TLabel").pack(anchor="w")

        btns = ttk.Frame(self.win, padding=(12, 6, 12, 12))
        btns.pack(side="bottom", fill="x")
        self.scan_label = ttk.Label(btns, text="", style="Muted.TLabel")
        self.scan_label.pack(side="left")
        ttk.Button(btns, text="Annuler", command=self.close).pack(side="right")
        self.btn_open = ttk.Button(btns, text="Ouvrir", command=self.open_selected)
        self.btn_open.pack(side="right", padx=(0, 6))
        ttk.Button(btns, text="Parcourir…", command=self.browse).pack(side="right", padx=(0, 6))
        self.btn_scan = ttk.Button(btns, text="Rechercher sur les disques", command=self.scan)
        self.btn_scan.pack(side="right", padx=(0, 6))

        body = ttk.Frame(self.win, padding=(12, 4))
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
        self.tree.tag_configure("section", font=app.font_bold, foreground=COLORS["accent"])
        self.tree.tag_configure("current", foreground="#7fd17f")
        self.tree.tag_configure("disabled", foreground=COLORS["muted"])
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self.tree.insert("", "end", iid="recent", text="Récentes", open=True, tags=("section",))
        self.tree.insert("", "end", iid="found", text="Détectées sur les disques", open=True, tags=("section",))
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
        self.tree.insert(parent, "end", iid=iid, text=name,
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
    """Base des petites fenêtres modales."""

    def __init__(self, app: "MainWindow", parent, title: str):
        self.app = app
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.configure(bg=COLORS["panel"])
        self.win.transient(parent)
        self.win.resizable(True, False)
        self.body = ttk.Frame(self.win, style="Panel.TFrame", padding=16)
        self.body.pack(fill="both", expand=True)

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
        super().__init__(app, parent, "Tags — iphotor")
        self.result: Optional[list[str]] = None
        b = self.body
        ttk.Label(b, text="Tags de la photo", style="Title.TLabel").pack(anchor="w")
        ttk.Label(b, text=safe_text(photo.filename or photo.label), style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.vars: dict[str, tk.BooleanVar] = {}
        names = sorted(set(known) | set(current), key=normalize_text)
        if names:
            ttk.Label(b, text="Tags existants :", style="Panel.TLabel").pack(anchor="w")
            box = ttk.Frame(b, style="Panel.TFrame")
            box.pack(anchor="w", fill="x", pady=(2, 10))
            cols = 3
            for i, name in enumerate(names):
                var = tk.BooleanVar(value=name in current)
                self.vars[name] = var
                ttk.Checkbutton(box, text=name, variable=var).grid(row=i // cols, column=i % cols,
                                                                   sticky="w", padx=(0, 18), pady=1)
        ttk.Label(b, text="Nouveaux tags (séparés par des virgules) :", style="Panel.TLabel").pack(anchor="w")
        self.new_var = tk.StringVar()
        entry = ttk.Entry(b, textvariable=self.new_var, width=48)
        entry.pack(anchor="w", fill="x", pady=(2, 12))
        btns = ttk.Frame(b, style="Panel.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="Enregistrer", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Annuler", command=self.win.destroy).pack(side="right", padx=(0, 6))
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
        self.win.geometry("640x620")
        self.win.minsize(420, 300)
        top = ttk.Frame(self.win, style="Panel.TFrame", padding=(12, 10))
        top.pack(side="top", fill="x")
        ttk.Label(top, text=safe_text(photo.filename or photo.label), style="Title.TLabel").pack(anchor="w")
        try:
            size = format_size(os.path.getsize(path))
        except OSError:
            size = "?"
        ttk.Label(top, text=f"{safe_text(path)}   ·   {size}", style="Muted.TLabel",
                  wraplength=600, justify="left").pack(anchor="w")

        btns = ttk.Frame(self.win, style="Panel.TFrame", padding=(12, 8))
        btns.pack(side="bottom", fill="x")
        ttk.Button(btns, text="Fermer", command=self.win.destroy).pack(side="right")
        ttk.Button(btns, text="Copier le texte", command=self.copy_text).pack(side="right", padx=(0, 6))
        if data.get("gps"):
            ttk.Button(btns, text="Voir sur une carte", command=self.open_map).pack(side="left")

        frame = ttk.Frame(self.win, style="Panel.TFrame", padding=(12, 0))
        frame.pack(side="top", fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("v",), show="tree", selectmode="browse")
        self.tree.column("#0", width=230, stretch=False)
        self.tree.column("v", width=380, stretch=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("section", font=app.font_bold, foreground=COLORS["accent"])
        self.tree.tag_configure("error", foreground=COLORS["missing"])

        if data.get("error"):
            self.tree.insert("", "end", text="⚠ " + data["error"], tags=("error",))
        if data.get("summary"):
            sec = self.tree.insert("", "end", text="Résumé", tags=("section",), open=True)
            for label, value in data["summary"]:
                self.tree.insert(sec, "end", text=label, values=(value,))
        for group, items in data.get("groups", []):
            sec = self.tree.insert("", "end", text=f"{group} ({len(items)})", tags=("section",), open=False)
            for name, value in items:
                self.tree.insert(sec, "end", text=name, values=(value,))
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
        super().__init__(app, parent, "Copier vers un dossier — iphotor")
        self.result: Optional[dict] = None
        st = app.settings
        b = self.body
        ttk.Label(b, text=f"Copier {what}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(b, text=summary, style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(b, text="Dossier de destination :", style="Panel.TLabel").pack(anchor="w")
        row = ttk.Frame(b, style="Panel.TFrame")
        row.pack(fill="x", pady=(2, 10))
        default = st.copy_dest if st.copy_dest and os.path.isdir(st.copy_dest) else ""
        self.dest_var = tk.StringVar(value=default)
        self.entry = ttk.Entry(row, textvariable=self.dest_var, width=56)
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Parcourir…", command=self.browse).pack(side="left", padx=(6, 0))
        self.sub_var = tk.BooleanVar(value=bool(subdir_name))
        self.subdir_name = subdir_name
        if subdir_name:
            ttk.Checkbutton(b, text=f"Créer un sous-dossier « {safe_filename(subdir_name)} »",
                            variable=self.sub_var).pack(anchor="w")
        self.orig_var = tk.BooleanVar(value=st.copy_original)
        if is_library:
            ttk.Label(b, text="Fichier copié :", style="Panel.TLabel").pack(anchor="w", pady=(8, 0))
            ttk.Radiobutton(b, text="Version affichée (la version retouchée si elle existe)",
                            variable=self.orig_var, value=False).pack(anchor="w")
            ttk.Radiobutton(b, text="Fichier original (tel que sorti de l'appareil, RAW compris)",
                            variable=self.orig_var, value=True).pack(anchor="w")
        self.open_var = tk.BooleanVar(value=st.copy_open)
        ttk.Checkbutton(b, text="Ouvrir le dossier de destination à la fin",
                        variable=self.open_var).pack(anchor="w", pady=(8, 0))
        ttk.Label(b, text="Les fichiers existants ne sont jamais remplacés : un numéro est ajouté au nom "
                          "si besoin.\nLes fichiers d'origine ne sont ni modifiés ni déplacés.",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(12, 12))
        btns = ttk.Frame(b, style="Panel.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="Copier", command=self.ok).pack(side="right")
        ttk.Button(btns, text="Annuler", command=self.win.destroy).pack(side="right", padx=(0, 6))
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
        b = ttk.Frame(self.win, style="Panel.TFrame", padding=16)
        b.pack(fill="both", expand=True)
        ttk.Label(b, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(b, text=f"vers {safe_text(task.dest)}", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.bar = ttk.Progressbar(b, mode="determinate", length=420, maximum=max(1, task.total))
        self.bar.pack(fill="x")
        self.label = ttk.Label(b, text="Préparation…", style="Panel.TLabel")
        self.label.pack(anchor="w", pady=(6, 10))
        self.btn = ttk.Button(b, text="Annuler", command=self.cancel)
        self.btn.pack(anchor="e")
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
        self._last_grid_msg: tuple[str, str] = ("", COLORS["muted"])

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

        self._setup_style()
        self._build_ui()
        self._update_title()
        self.root.after(30, self._poll_thumbnails)
        self.root.after(100, lambda: self.start(force_reindex))

    # -- style ---------------------------------------------------------------
    def _setup_style(self) -> None:
        root = self.root
        root.configure(bg=COLORS["bg"])
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{min(1500, int(sw * 0.9))}x{min(950, int(sh * 0.88))}")
        root.minsize(900, 560)

        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=10)
        self.font = base
        self.font_small = tkfont.Font(family=base.cget("family"), size=9)
        self.font_bold = tkfont.Font(family=base.cget("family"), size=10, weight="bold")
        self.font_title = tkfont.Font(family=base.cget("family"), size=13, weight="bold")
        self.font_big = tkfont.Font(family=base.cget("family"), size=12)

        st = ttk.Style(root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        c = COLORS
        st.configure(".", background=c["panel"], foreground=c["text"], fieldbackground=c["tile"],
                     bordercolor=c["bg"], lightcolor=c["panel"], darkcolor=c["panel"])
        st.configure("TFrame", background=c["panel"])
        st.configure("Panel.TFrame", background=c["panel"])
        st.configure("Bg.TFrame", background=c["bg"])
        st.configure("TLabel", background=c["panel"], foreground=c["text"])
        st.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        st.configure("Muted.TLabel", background=c["panel"], foreground=c["muted"])
        st.configure("Favorite.TLabel", background=c["panel"], foreground=c["favorite"])
        st.configure("Title.TLabel", background=c["panel"], foreground=c["text"], font=self.font_title)
        st.configure("Status.TLabel", background=c["bg"], foreground=c["muted"], font=self.font_small, padding=(8, 3))
        st.configure("TButton", background=c["tile"], foreground=c["text"], padding=(10, 4), borderwidth=0)
        st.map("TButton", background=[("disabled", c["panel"]), ("active", c["tile_hover"])],
               foreground=[("disabled", "#666")])
        st.configure("TEntry", fieldbackground=c["tile"], foreground=c["text"], insertcolor=c["text"], padding=4)
        st.configure("TCombobox", fieldbackground=c["tile"], foreground=c["text"], background=c["tile"],
                     arrowcolor=c["text"], padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", c["tile"])], foreground=[("readonly", c["text"])])
        root.option_add("*TCombobox*Listbox.background", c["tile"])
        root.option_add("*TCombobox*Listbox.foreground", c["text"])
        st.configure("Treeview", background=c["panel"], fieldbackground=c["panel"], foreground=c["text"],
                     rowheight=24, borderwidth=0)
        st.map("Treeview", background=[("selected", c["accent"])], foreground=[("selected", "white")])
        st.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"], borderwidth=0)
        st.configure("Vertical.TScrollbar", background=c["tile"], troughcolor=c["panel"], arrowcolor=c["muted"],
                     borderwidth=0)
        st.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["tile"])
        st.configure("Mode.Toolbutton", background=c["tile"], foreground=c["text"], padding=(12, 4), borderwidth=0)
        st.map("Mode.Toolbutton", background=[("selected", c["accent"]), ("active", c["tile_hover"])],
               foreground=[("selected", "white")])
        st.configure("TCheckbutton", background=c["panel"], foreground=c["text"])
        st.map("TCheckbutton", background=[("active", c["panel"])])
        st.configure("Toolbar.TFrame", background=c["bg"])
        st.configure("Toolbar.TLabel", background=c["bg"], foreground=c["muted"])
        st.configure("Toolbar.TCheckbutton", background=c["bg"], foreground=c["text"])
        st.map("Toolbar.TCheckbutton", background=[("active", c["bg"])])

    # -- construction de l'interface ---------------------------------------
    def _build_ui(self) -> None:
        root = self.root
        menubar = tk.Menu(root)
        m_file = tk.Menu(menubar, tearoff=False)
        m_file.add_command(label="Ouvrir une bibliothèque…", command=self.open_library_dialog,
                           accelerator="Ctrl+O")
        self.m_recent = tk.Menu(m_file, tearoff=False)
        m_file.add_cascade(label="Bibliothèques récentes", menu=self.m_recent)
        self._rebuild_recent_menu()
        m_file.add_command(label="Ouvrir un dossier…", command=self.open_folder_dialog,
                           accelerator="Ctrl+Maj+O")
        m_file.add_separator()
        m_file.add_command(label="Copier l'élément sélectionné vers un dossier…",
                           command=self.copy_selected_tree_item, accelerator="Ctrl+Maj+S")
        m_file.add_separator()
        m_file.add_command(label="Recharger", command=self.reload_current, accelerator="F5")
        m_file.add_command(label="Reconstruire l'index de la bibliothèque", command=self.rebuild_index)
        m_file.add_separator()
        m_file.add_command(label="Vider le cache des miniatures", command=self.clear_thumb_cache)
        m_file.add_command(label="Informations sur le cache", command=self.show_cache_info)
        m_file.add_separator()
        m_file.add_command(label="Quitter", command=self.quit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="Fichier", menu=m_file)
        m_help = tk.Menu(menubar, tearoff=False)
        m_help.add_command(label="Raccourcis clavier", command=self.show_shortcuts)
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
        root.protocol("WM_DELETE_WINDOW", self.quit)

        # Barre des modes
        toolbar = ttk.Frame(root, style="Toolbar.TFrame", padding=(12, 6))
        toolbar.pack(side="top", fill="x")
        self.mode_var = tk.StringVar(value=MODE_LIBRARY)
        ttk.Radiobutton(toolbar, text="📚  Bibliothèque iPhoto", value=MODE_LIBRARY, variable=self.mode_var,
                        style="Mode.Toolbutton", command=lambda: self.set_mode(self.mode_var.get())).pack(side="left")
        ttk.Radiobutton(toolbar, text="🗂  Explorer un dossier", value=MODE_FOLDER, variable=self.mode_var,
                        style="Mode.Toolbutton", command=lambda: self.set_mode(self.mode_var.get())).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="📂  Ouvrir un dossier…", command=self.open_folder_dialog).pack(side="left", padx=(16, 0))
        self.folder_opts = ttk.Frame(toolbar, style="Toolbar.TFrame")
        self.recursive_var = tk.BooleanVar(value=self.settings.folder_recursive)
        ttk.Checkbutton(self.folder_opts, text="Inclure les sous-dossiers", variable=self.recursive_var,
                        command=self._on_recursive_toggle, style="Toolbar.TCheckbutton").pack(side="left")
        ttk.Label(self.folder_opts, text="Trier par :", style="Toolbar.TLabel").pack(side="left", padx=(16, 4))
        labels = [lbl for lbl, _k, _d in FOLDER_SORTS]
        self.sort_var = tk.StringVar(value=self.settings.folder_sort if self.settings.folder_sort in labels else labels[0])
        self.sort_box = ttk.Combobox(self.folder_opts, textvariable=self.sort_var, state="readonly",
                                     width=30, values=labels)
        self.sort_box.pack(side="left")
        self.sort_box.bind("<<ComboboxSelected>>", self._on_sort_change)
        key, desc = self._sort_choice()
        self.folder.sort_key, self.folder.sort_desc = key, desc

        # Barre supérieure
        top = ttk.Frame(root, style="Panel.TFrame", padding=(12, 8))
        top.pack(side="top", fill="x")
        left = ttk.Frame(top, style="Panel.TFrame")   # placé après la recherche (voir plus bas)
        self.title_label = ttk.Label(left, text="iphotor", style="Title.TLabel")
        self.title_label.pack(anchor="w")
        self.subtitle_label = ttk.Label(left, text="", style="Muted.TLabel")
        self.subtitle_label.pack(anchor="w")

        nav = ttk.Frame(top, style="Panel.TFrame")
        nav.pack(side="right")
        self.btn_prev = ttk.Button(nav, text="◀  Précédent", command=self.prev_page)
        self.btn_prev.pack(side="left")
        self.page_label = ttk.Label(nav, text="", style="Panel.TLabel", width=16, anchor="center")
        self.page_label.pack(side="left", padx=6)
        self.btn_next = ttk.Button(nav, text="Suivant  ▶", command=self.next_page)
        self.btn_next.pack(side="left")

        search = ttk.Frame(top, style="Panel.TFrame")
        search.pack(side="right", padx=(0, 24))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.search_var, width=30)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Return>", lambda e: self.do_search())
        self.search_entry.bind("<Escape>", lambda e: self.clear_search())
        self.scope_var = tk.StringVar(value="Toute la photothèque")
        self.scope = ttk.Combobox(search, textvariable=self.scope_var, state="readonly", width=20,
                                  values=("Toute la photothèque", "Album sélectionné"))
        self.scope.pack(side="left", padx=(6, 0))
        self.btn_search = ttk.Button(search, text="Rechercher", command=self.do_search)
        self.btn_search.pack(side="left", padx=(6, 0))
        ttk.Button(search, text="✕", width=3, command=self.clear_search).pack(side="left", padx=(4, 0))
        # Le titre prend la place restante : un long chemin n'écrase plus la recherche.
        left.pack(side="left", fill="x", expand=True)

        # Barre d'état
        self.status_var = tk.StringVar(value="")
        status = ttk.Label(root, textvariable=self.status_var, style="Status.TLabel", anchor="w")
        status.pack(side="bottom", fill="x")

        # Corps : arborescence + grille
        body = tk.PanedWindow(root, orient="horizontal", sashwidth=5, bg=COLORS["bg"], bd=0)
        body.pack(side="top", fill="both", expand=True)

        left_pane = ttk.Frame(body, style="Panel.TFrame")
        tree_frame = ttk.Frame(left_pane, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True)
        self.tree_frame = tree_frame
        self.tree = ttk.Treeview(tree_frame, columns=("n",), show="tree", selectmode="browse")
        self.tree.column("#0", width=380, stretch=True)
        self.tree.column("n", width=60, anchor="e", stretch=False)
        tsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.tag_configure("section", font=self.font_bold, foreground=COLORS["accent"])
        self.tree.tag_configure("empty", foreground=COLORS["muted"])
        self.tree.tag_configure("folder", foreground="#d8c88a")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", lambda e: self._on_tree_menu(e, self.tree))

        self.ftree_frame = ttk.Frame(left_pane, style="Panel.TFrame")
        self.ftree = ttk.Treeview(self.ftree_frame, columns=("n",), show="tree", selectmode="browse")
        self.ftree.column("#0", width=380, stretch=True)
        self.ftree.column("n", width=60, anchor="e", stretch=False)
        fsb = ttk.Scrollbar(self.ftree_frame, orient="vertical", command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=fsb.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.ftree.tag_configure("section", font=self.font_bold, foreground=COLORS["accent"])
        self.ftree.tag_configure("folder", foreground="#d8c88a")
        self.ftree.tag_configure("error", foreground=COLORS["missing"])
        self.ftree.bind("<<TreeviewSelect>>", self._on_ftree_select)
        self.ftree.bind("<Button-3>", lambda e: self._on_tree_menu(e, self.ftree))
        body.add(left_pane, minsize=220, width=460)

        grid_frame = ttk.Frame(body, style="Bg.TFrame")
        self.canvas = tk.Canvas(grid_frame, bg=COLORS["bg"], highlightthickness=0)
        gsb = ttk.Scrollbar(grid_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=gsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        gsb.pack(side="right", fill="y")
        body.add(grid_frame, minsize=400)

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

        # Indicateur de chargement (au centre de la grille)
        self.progress = ttk.Progressbar(grid_frame, mode="indeterminate", length=260)
        self._update_nav()

    def _focus_in_entry(self) -> bool:
        return self.root.focus_get() is self.search_entry

    # -- choix de la bibliothèque -----------------------------------------------
    def _update_title(self) -> None:
        name = os.path.basename(self.library.root) or self.library.root
        if self.mode == MODE_FOLDER and not name:
            name = "Explorer un dossier"
        name = safe_text(name)
        self.root.title(f"{name} — iphotor {APP_VERSION}")

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
        self._rebuild_recent_menu()
        self._update_title()
        self.search_var.set("")
        self.current_album_id = None
        self.status_var.set("")
        self.start(False)

    # -- modes : bibliothèque iPhoto / explorer un dossier ---------------------------
    # Attributs propres à chaque mode, sauvegardés et restaurés lors d'un changement de mode.
    _MODE_ATTRS = ("library", "cache", "loader", "photos", "view", "total", "page_no",
                   "view_ids", "current_album_id", "_annot_sel", "_tag_names")

    def _initial_folder_state(self) -> dict:
        lib = FolderLibrary("")
        cache = FolderThumbCache(lib)
        return {
            "library": lib, "cache": cache, "loader": ThumbnailLoader(lib, cache),
            "photos": None, "view": View("empty", title="Explorer un dossier"), "total": 0,
            "page_no": 0, "view_ids": None, "current_album_id": None, "search": "",
            "_annot_sel": None, "_tag_names": [],
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
        new = self._mode_states.get(mode) or self._initial_folder_state()
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
        if self.mode == MODE_FOLDER:
            self.tree_frame.pack_forget()
            self.ftree_frame.pack(fill="both", expand=True)
            self.scope.pack_forget()
            self.folder_opts.pack(side="left", padx=(24, 0))
            self.search_entry.configure(width=36)
        else:
            self.ftree_frame.pack_forget()
            self.tree_frame.pack(fill="both", expand=True)
            self.folder_opts.pack_forget()
            self.scope.pack(side="left", padx=(6, 0), before=self.btn_search)
            self.search_entry.configure(width=30)
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
        self.settings.save()

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
        t.insert("", "end", iid="f:", text="🗂  " + safe_text(name), values=(format_count(totals[""]),),
                 tags=("section",), open=True)
        for rel in sorted((k for k in totals if k), key=lambda k: (k.count("/"), natural_key(k))):
            parent = rel.rpartition("/")[0]
            t.insert("f:" + parent, "end", iid="f:" + rel, text="📁 " + rel.rpartition("/")[2],
                     values=(format_count(totals[rel]),), tags=("folder",))
        if f.errors:
            t.insert("", "end", iid="err", text="⚠  Dossiers inaccessibles", values=(format_count(len(f.errors)),),
                     tags=("error",))
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
        if iid == "fav" or iid.startswith("tag:"):
            self._show_annotation_view("fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])]))
            return
        if iid == "sec:tags":
            self.ftree.item(iid, open=True)
            return
        self._annot_sel = None
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
        t.insert("", "end", iid="all", text="📷  Toutes les photos", values=(format_count(total),), tags=("section",))

        # Albums (liste à plat, avec le chemin des dossiers pour lever les homonymies)
        flat = am.albums_flat()
        t.insert("", "end", iid="sec:albums", text="🖼  Albums", values=(format_count(len(flat)),), tags=("section",))
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
            icon = "⚙ " if a.kind == KIND_SMART else ""
            t.insert("sec:albums", "end", iid=f"a:{a.id}:flat", text=icon + label,
                     values=(format_count(a.photo_count),), tags=("empty",) if a.photo_count == 0 else ())

        # Événements, groupés par année
        groups = am.events_by_year()
        n_events = sum(len(e) for _, e in groups)
        t.insert("", "end", iid="sec:events", text="📅  Événements", values=(format_count(n_events),), tags=("section",))
        for year, events in groups:
            yid = f"y:{year if year is not None else 'none'}"
            ycount = sum(e.photo_count for e in events)
            t.insert("sec:events", "end", iid=yid,
                     text=str(year) if year is not None else "Date inconnue",
                     values=(format_count(ycount),))
            for e in events:
                t.insert(yid, "end", iid=f"a:{e.id}:ev", text=e.name,
                         values=(format_count(e.photo_count),), tags=("empty",) if e.photo_count == 0 else ())

        # Dossiers : hiérarchie complète
        t.insert("", "end", iid="sec:folders", text="📁  Dossiers", values=("",), tags=("section",))

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
                    t.insert(parent_iid, "end", iid=iid, text="📁 " + name,
                             values=(format_count(child.photo_count),), tags=("folder",))
                    add_children(iid, child.id)
                    n += 1
                elif child.kind in (KIND_ALBUM, KIND_SMART, KIND_SPECIAL) and parent_id is not None:
                    icon = "⚙ " if child.kind == KIND_SMART else ""
                    t.insert(parent_iid, "end", iid=f"a:{child.id}:tree", text=icon + name,
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
        if iid == "fav" or iid.startswith("tag:"):
            self._show_annotation_view("fav" if iid == "fav" else ("tag", self._tag_names[int(iid[4:])]))
            self._set_status(self.tree.item(iid, "text").strip())
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
            self.total = self.photos.count(view) if (self.photos and view.kind != "empty") else 0
        except sqlite3.Error as exc:
            self.total = 0
            message = f"Erreur de lecture de l'index : {exc}"
        self.title_label.config(text=view.title or "iphotor")
        sub = plural(self.total, "photo") if view.kind != "empty" else ""
        if view.kind == "folder":
            sub = self._folder_subtitle()
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
                elif self.view.kind == "folder":
                    if self.folder.query:
                        message = f"Aucune image dont le nom contient « {self.folder.query} »."
                    else:
                        message = ("Aucune image (JPG, PNG, GIF, BMP, TIFF) dans ce dossier."
                                   + ("" if self.folder.recursive else
                                      "\nCochez « Inclure les sous-dossiers » pour chercher plus loin."))
            self._show_grid_message(message)
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
        return margin + (index % cols) * TILE_W, 8 + (index // cols) * TILE_H

    def _layout(self) -> None:
        """Dessine (ou redessine) toutes les vignettes de la page."""
        self._layout_job = None
        self.canvas.delete("all")
        if not self.page_photos:
            return
        for i, p in enumerate(self.page_photos):
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
        bg = COLORS["tile_hover"] if self.hover_index == i else COLORS["tile"]
        c.create_rectangle(x + 4, y + 4, x + TILE_W - 4, y + TILE_H - 4, fill=bg, outline="",
                           tags=(tag, f"bg{i}"))
        cx, cy = x + TILE_W // 2, y + 8 + THUMB_SIZE // 2
        if isinstance(state, dict) and state.get("img") is not None:
            c.create_image(cx, cy, image=state["img"], anchor="center", tags=(tag,))
        else:
            c.create_rectangle(cx - THUMB_SIZE // 2 + 8, cy - THUMB_SIZE // 2 + 8,
                               cx + THUMB_SIZE // 2 - 8, cy + THUMB_SIZE // 2 - 8,
                               fill=COLORS["placeholder"], outline="", tags=(tag,))
            label = "…"
            if isinstance(state, dict):
                label = {"video": "▶\nVidéo", "raw": "RAW\n(sans aperçu)", "missing": "Image\nintrouvable",
                         "error": "Image\nillisible"}.get(state.get("status", ""), "Aucun\naperçu")
            c.create_text(cx, cy, text=label, fill=COLORS["muted"], font=self.font_small,
                          justify="center", tags=(tag,))
        if p.is_video:
            c.create_rectangle(x + 10, y + 10, x + 62, y + 28, fill=COLORS["video"], outline="", tags=(tag,))
            c.create_text(x + 36, y + 19, text="▶ VIDÉO", fill="black", font=self.font_small, tags=(tag,))
        if isinstance(state, dict) and state.get("exists") is False:
            c.create_rectangle(x + 10, cy + THUMB_SIZE // 2 - 22, x + TILE_W - 10, cy + THUMB_SIZE // 2 - 4,
                               fill=COLORS["missing"], outline="", tags=(tag,))
            c.create_text(cx, cy + THUMB_SIZE // 2 - 13, text="⚠ Fichier introuvable", fill="white",
                          font=self.font_small, tags=(tag,))
        if p.rating:
            c.create_text(x + TILE_W - 12, y + 18, text="★" * min(p.rating, 5), fill="#ffd34d",
                          anchor="e", font=self.font_small, tags=(tag,))
        if self.is_favorite(p):
            c.create_text(x + TILE_W - 12, y + THUMB_SIZE - 2, text="♥", fill=COLORS["favorite"],
                          anchor="e", font=self.font_title, tags=(tag,))
        text_w = TILE_W - 18
        c.create_text(cx, y + THUMB_SIZE + 22, text=self._truncate(p.label, self.font, text_w),
                      fill=COLORS["text"], font=self.font, tags=(tag,))
        sub = format_date(p.date, with_time=False) if p.date is not None else ""
        if p.label != p.filename and p.filename:
            sub = (sub + "  ·  " if sub else "") + p.filename
        if p.source == "folder":
            sub = (sub + "  ·  " if sub else "") + format_size(p.size)
        c.create_text(cx, y + THUMB_SIZE + 40, text=self._truncate(sub, self.font_small, text_w),
                      fill=COLORS["muted"], font=self.font_small, tags=(tag,))

    def _redraw_tile(self, i: int) -> None:
        if 0 <= i < len(self.page_photos):
            self.canvas.delete(f"t{i}")
            self._draw_tile(i, self.page_photos[i])

    def _show_grid_message(self, text: str, color: str = COLORS["muted"]) -> None:
        self._last_grid_msg = (text, color)
        self.canvas.delete("all")
        if not text:
            return
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        self.canvas.create_text(w // 2, h // 2 - 40, text=text, fill=color, font=self.font_big,
                                justify="center", width=w - 60, tags=("msg",))
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
                self.canvas.coords(msg[0], w // 2, h // 2 - 40)
                self.canvas.itemconfigure(msg[0], width=w - 60)

    def _index_at(self, event) -> Optional[int]:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        cols = self._columns()
        width = self.canvas.winfo_width()
        margin = max(5, (width - cols * TILE_W) // 2)
        col = int((x - margin) // TILE_W)
        row = int((y - 8) // TILE_H)
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
            self.canvas.itemconfigure(f"bg{old}", fill=COLORS["tile"])
        if i is not None:
            self.canvas.itemconfigure(f"bg{i}", fill=COLORS["tile_hover"])
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

    def _annotation_parts(self, p: Photo) -> list[str]:
        parts = ["♥ favori"] if self.is_favorite(p) else []
        tags = self.tags_of(p)
        if tags:
            parts.append("🏷 " + ", ".join(tags))
        return parts

    def _on_canvas_motion(self, event) -> None:
        self._set_hover(self._index_at(event))

    def _on_canvas_click(self, event) -> None:
        i = self._index_at(event)
        if i is None:
            return
        self.open_viewer(self.page_no * PAGE_SIZE + i)

    def _poll_thumbnails(self) -> None:
        processed = 0
        try:
            while processed < 40:
                gen, index, _pid, img, exists, status = self.loader.results.get_nowait()
                processed += 1
                if gen != self.generation:
                    continue   # résultat d'une page qui n'est plus affichée
                tkimg = ImageTk.PhotoImage(img) if img is not None else None
                self.tile_images[index] = {"img": tkimg, "exists": exists, "status": status}
                self._redraw_tile(index)
        except queue.Empty:
            pass
        self.root.after(30 if processed else 60, self._poll_thumbnails)

    # -- visionneuse -------------------------------------------------------------
    def open_viewer(self, absolute_index: int) -> None:
        if self.photos is None:
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
        if page != self.page_no and 0 <= page < self.page_count():
            self.page_no = page
            self.load_page()

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
        t.insert("", 0, iid="search", text=f"🔍  Résultats : « {q} »", values=(format_count(n_photos),),
                 tags=("section",), open=True)
        found = self.albums.search(q)
        for a in found:
            kind = {KIND_FOLDER: "📁 ", KIND_EVENT: "📅 ", KIND_SMART: "⚙ "}.get(a.kind, "🖼 ")
            label = kind + a.name
            path = self.albums.path_label(a)
            if path:
                label += f"   ({path})"
            elif a.kind == KIND_EVENT and a.year:
                label += f"   ({a.year})"
            t.insert("search", "end", iid=f"s:a:{a.id}", text=label, values=(format_count(a.photo_count),))
        self._set_status(f"Recherche « {q} » : {plural(n_photos, 'photo')}, "
                         f"{plural(len(found), 'album ou événement', 'albums ou événements')} trouvés.")
        t.see("search")
        t.selection_set("search")   # déclenche set_view via _on_tree_select

    def clear_search(self) -> None:
        self.search_var.set("")
        if self.mode == MODE_FOLDER:
            self._folder_search()
            return
        if self.tree.exists("search"):
            self.tree.delete("search")
            if self.tree.exists("all"):
                self.tree.selection_set("all")

    # -- favoris et tags -----------------------------------------------------------
    def _scope(self) -> str:
        return FILES_SCOPE if self.mode == MODE_FOLDER else library_scope(self.library.root)

    def _key(self, p: Photo) -> tuple[str, str]:
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
        for iid in ("fav", "sec:tags"):
            if t.exists(iid):
                t.delete(iid)
        t.insert("", pos, iid="fav", text="♥  Mes favoris", values=(format_count(self._annotation_count(favs)),),
                 tags=("section",))
        self._tag_names = tags
        if tags:
            t.insert("", pos + 1, iid="sec:tags", text="🏷  Tags", values=(format_count(len(tags)),),
                     tags=("section",), open=bool(was_open))
            for i, (name, keys) in enumerate(zip(tags, tag_keys)):
                t.insert("sec:tags", "end", iid=f"tag:{i}", text=name,
                         values=(format_count(self._annotation_count(keys)),))
        # Resélectionne l'entrée courante sans recharger la grille.
        target = None
        if self._annot_sel == "fav":
            target = "fav"
        elif isinstance(self._annot_sel, tuple) and self._annot_sel[1] in tags:
            target = f"tag:{tags.index(self._annot_sel[1])}"
        elif sel and t.exists(sel[0]) and sel[0] != "sec:tags" and not sel[0].startswith("tag:"):
            target = sel[0]
        if target:
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
            m.add_command(label="Ouvrir", command=open_cmd)
            m.add_separator()
        m.add_command(label="Copier l'image", command=lambda: self.copy_image(p, parent), accelerator="Ctrl+C")
        m.add_command(label="Copier le chemin du fichier", command=lambda: self.copy_path(p),
                      accelerator="Ctrl+Maj+C")
        m.add_command(label="Copier vers un dossier…", command=lambda: self.copy_photo_to(p, parent),
                      accelerator="Ctrl+S")
        m.add_separator()
        fav = self.is_favorite(p)
        m.add_command(label="♥  Retirer des favoris" if fav else "♡  Ajouter aux favoris",
                      command=lambda: self.toggle_favorite(p, parent), accelerator="F")
        m.add_command(label="🏷  Tags…", command=lambda: self.edit_tags(p, parent), accelerator="T")
        m.add_separator()
        m.add_command(label="Informations EXIF…", command=lambda: self.show_exif(p, parent), accelerator="I")
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    def _on_canvas_menu(self, event) -> None:
        i = self._index_at(event)
        if i is None:
            return
        self._set_hover(i)
        p = self.page_photos[i]
        absolute = self.page_no * PAGE_SIZE + i
        self.photo_menu(p, self.root, event.x_root, event.y_root, lambda: self.open_viewer(absolute))

    def _hovered_photo(self) -> Optional[Photo]:
        if self.hover_index is not None and self.hover_index < len(self.page_photos):
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
        if self.mode == MODE_FOLDER:
            ok = iid.startswith("f:") or iid == "fav" or iid.startswith("tag:")
        else:
            ok = (iid in ("all", "fav", "search") or iid.startswith("tag:") or iid.startswith("y:")
                  or self._album_from_iid(iid) is not None)
        if not ok:
            return
        label = tree.item(iid, "text").strip().lstrip("📷🖼📅📁🗂♥🏷🔍⚙ ").strip()
        if len(label) > 50:
            label = label[:49] + "…"
        m = tk.Menu(tree, tearoff=False)
        m.add_command(label=f"Copier « {label} » vers un dossier…", command=lambda: self.copy_tree_item(iid))
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
