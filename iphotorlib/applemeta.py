"""Mots-clés, personnes (visages nommés) et lieux des bases Apple — lecture seule, schéma détecté.

Bases recherchées (selon la génération de bibliothèque) :
  - Photos 1 à 4 : database/photos.db (ou Library.apdb)
  - iPhoto 9.x   : Database/apdb/Library.apdb, Database/apdb/Properties.apdb, Database/apdb/Faces.db
                   (et les mêmes noms directement dans Database/ pour certaines versions)

Chaque base est COPIÉE dans le cache (avec son journal WAL) avant d'être ouverte : l'original
n'est jamais ouvert par SQLite. Les tables et colonnes sont détectées ; une information absente
est simplement ignorée (rien n'est inventé). Les termes sont rattachés aux photos par l'UUID de
version (RKVersion.uuid), qui est le GUID de l'index d'iphotor.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import storage

CANDIDATES = [
    "database/photos.db", "database/Library.apdb",
    "Database/apdb/Library.apdb", "Database/apdb/Properties.apdb", "Database/apdb/Faces.db",
    "Database/Library.apdb", "Database/Properties.apdb", "Database/Faces.db",
]


@dataclass
class ImportResult:
    keywords: list = field(default_factory=list)    # (clé, mot-clé)
    persons: list = field(default_factory=list)     # (clé, personne)
    places: list = field(default_factory=list)      # (clé, lieu)
    sources: list = field(default_factory=list)     # bases lues
    notes: list = field(default_factory=list)       # informations / limites

    def summary(self) -> str:
        def n(rows):
            return len({v for _k, v in rows})
        return (f"{n(self.keywords)} mots-clés ({len(self.keywords)} associations), "
                f"{n(self.persons)} personnes ({len(self.persons)}), {n(self.places)} lieux ({len(self.places)})")


def find_databases(root: str) -> list[str]:
    out = []
    for rel in CANDIDATES:
        p = os.path.join(root, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                out.append(p)
        except OSError:
            pass
    return out


def _first(cols: set, *names: str) -> Optional[str]:
    return next((n for n in names if n in cols), None)


def import_terms(root: str, workdir: str, protected: Iterable[str]) -> ImportResult:
    res = ImportResult()
    dbs = find_databases(root)
    if not dbs:
        res.notes.append("Aucune base Apple (Library.apdb, photos.db, Faces.db…) dans cette bibliothèque : "
                         "seules les informations d'AlbumData.xml sont disponibles.")
        return res
    protected = list(protected) + [root]
    version_uuid: dict[int, str] = {}           # RKVersion.modelId -> uuid
    master_versions: dict[int, list[str]] = {}  # RKMaster.modelId -> [uuid de version]
    master_uuid_versions: dict[str, list[str]] = {}
    pending_faces: list[tuple[str, str]] = []   # (masterUuid, nom) issus de Faces.db

    # La base contenant RKVersion d'abord (elle fournit la correspondance des identifiants)
    dbs.sort(key=lambda p: 0 if os.path.basename(p) in ("photos.db", "Library.apdb") else 1)
    for path in dbs:
        try:
            with storage.SourceDBCopy(path, workdir, protected) as con:
                t = storage.tables(con)
                res.sources.append(os.path.relpath(path, root))
                if "RKVersion" in t:
                    vc = storage.columns(con, "RKVersion")
                    mcol = _first(vc, "masterId")
                    for r in con.execute(f"SELECT modelId, uuid{', ' + mcol if mcol else ''} FROM RKVersion"):
                        version_uuid[r[0]] = r[1]
                        if mcol and r[2] is not None:
                            master_versions.setdefault(r[2], []).append(r[1])
                    if "RKMaster" in t and mcol:
                        for r in con.execute("SELECT modelId, uuid FROM RKMaster"):
                            if r[0] in master_versions:
                                master_uuid_versions[r[1]] = master_versions[r[0]]
                _keywords(con, t, version_uuid, res)
                _places(con, t, version_uuid, res)
                _persons(con, t, version_uuid, master_versions, pending_faces, res)
        except (sqlite3.Error, OSError) as exc:
            res.notes.append(f"{os.path.relpath(path, root)} illisible : {exc}")
    for muuid, name in pending_faces:
        for vu in master_uuid_versions.get(muuid, []):
            res.persons.append((vu, name))
    if pending_faces and not master_uuid_versions:
        res.notes.append("Visages trouvés mais impossibles à rattacher aux photos (table RKMaster absente).")
    for attr in ("keywords", "persons", "places"):
        setattr(res, attr, sorted(set(getattr(res, attr))))
    return res


def _keywords(con, t, version_uuid, res) -> None:
    if not {"RKKeyword", "RKKeywordForVersion"} <= t:
        return
    kc = storage.columns(con, "RKKeywordForVersion")
    vcol, kcol = _first(kc, "versionId"), _first(kc, "keywordId")
    if not vcol or not kcol:
        res.notes.append("RKKeywordForVersion : colonnes inattendues, mots-clés ignorés.")
        return
    names = {r[0]: r[1] for r in con.execute("SELECT modelId, name FROM RKKeyword") if r[1]}
    for r in con.execute(f"SELECT {vcol}, {kcol} FROM RKKeywordForVersion"):
        u, n = version_uuid.get(r[0]), names.get(r[1])
        if u and n:
            res.keywords.append((u, str(n)))


def _places(con, t, version_uuid, res) -> None:
    if "RKPlace" not in t:
        return
    pc = storage.columns(con, "RKPlace")
    ncol = _first(pc, "defaultName", "name", "displayName")
    if not ncol:
        return
    names = {r[0]: r[1] for r in con.execute(f"SELECT modelId, {ncol} FROM RKPlace") if r[1]}
    if "RKPlaceForVersion" in t:
        fc = storage.columns(con, "RKPlaceForVersion")
        vcol, pcol = _first(fc, "versionId"), _first(fc, "placeId")
        if vcol and pcol:
            for r in con.execute(f"SELECT {vcol}, {pcol} FROM RKPlaceForVersion"):
                u, n = version_uuid.get(r[0]), names.get(r[1])
                if u and n:
                    res.places.append((u, str(n)))
            return
    if "RKVersion" in t:
        vc = storage.columns(con, "RKVersion")
        pcol = _first(vc, "mainPlaceId", "placeId")
        if pcol:
            for r in con.execute(f"SELECT uuid, {pcol} FROM RKVersion WHERE {pcol} IS NOT NULL"):
                n = names.get(r[1])
                if n:
                    res.places.append((r[0], str(n)))


def _persons(con, t, version_uuid, master_versions, pending_faces, res) -> None:
    # Photos 1 à 4 : RKPerson (noms) + RKFace (personId, imageModelId = RKMaster ou RKVersion)
    if {"RKPerson", "RKFace"} <= t:
        pc = storage.columns(con, "RKPerson")
        ncol = _first(pc, "name", "displayName", "fullName")
        fc = storage.columns(con, "RKFace")
        pid = _first(fc, "personId")
        img = _first(fc, "imageModelId", "versionId", "masterId")
        if ncol and pid and img:
            names = {r[0]: r[1] for r in con.execute(f"SELECT modelId, {ncol} FROM RKPerson") if r[1]}
            for r in con.execute(f"SELECT {pid}, {img} FROM RKFace WHERE {pid} IS NOT NULL"):
                n = names.get(r[0])
                if not n:
                    continue
                if img == "versionId":
                    targets = [version_uuid.get(r[1])]
                else:     # imageModelId / masterId : identifiant de l'original
                    targets = master_versions.get(r[1]) or [version_uuid.get(r[1])]
                for u in targets:
                    if u:
                        res.persons.append((u, str(n)))
        return
    # iPhoto : Faces.db (RKDetectedFace masterUuid/faceKey + RKFaceName faceKey/name)
    if {"RKFaceName", "RKDetectedFace"} <= t:
        nc = storage.columns(con, "RKFaceName")
        dc = storage.columns(con, "RKDetectedFace")
        fk_n, name_c = _first(nc, "faceKey"), _first(nc, "name", "fullName")
        fk_d, mu = _first(dc, "faceKey"), _first(dc, "masterUuid")
        if fk_n and name_c and fk_d and mu:
            names = {r[0]: r[1] for r in con.execute(f"SELECT {fk_n}, {name_c} FROM RKFaceName") if r[1]}
            for r in con.execute(f"SELECT {mu}, {fk_d} FROM RKDetectedFace"):
                n = names.get(r[1])
                if n and r[0]:
                    pending_faces.append((r[0], str(n)))
