"""Dossiers : instantanés (fichiers ajoutés / disparus), listes récentes et favoris.

Les instantanés sont stockés dans le CACHE (…/dossiers/instantanes/<empreinte du chemin>.json) :
ils servent à signaler les changements depuis la dernière visite. Aucune écriture dans le
dossier consulté ; la surveillance est passive (relecture de la liste des fichiers à la demande).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import storage

MAX_RECENT = 12


def snapshot_path(cache_dir: str, root: str) -> str:
    h = hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(cache_dir, "instantanes", h + ".json")


@dataclass
class Diff:
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    previous: Optional[float] = None      # date de l'instantané précédent

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} nouvelle{'s' if len(self.added) > 1 else ''}")
        if self.removed:
            parts.append(f"{len(self.removed)} disparue{'s' if len(self.removed) > 1 else ''}")
        if self.changed:
            parts.append(f"{len(self.changed)} modifiée{'s' if len(self.changed) > 1 else ''}")
        return ", ".join(parts)


def entries_signature(entries: Iterable[tuple]) -> dict:
    """entries : (chemin, sous-dossier, nom, mtime, taille) → {chemin: "taille:mtime"}."""
    return {e[0]: f"{e[4]}:{int(e[3])}" for e in entries}


def compare(old: dict, new: dict) -> Diff:
    d = Diff()
    for p, sig in new.items():
        o = old.get(p)
        if o is None:
            d.added.append(p)
        elif o != sig:
            d.changed.append(p)
    d.removed = [p for p in old if p not in new]
    return d


def load_snapshot(cache_dir: str, root: str) -> tuple[Optional[dict], Optional[float], bool]:
    """(signatures, date, sous-dossiers inclus) du dernier instantané, ou (None, None, False)."""
    try:
        with open(snapshot_path(cache_dir, root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("files", {}), data.get("time"), bool(data.get("recursive"))
    except (OSError, ValueError):
        return None, None, False


def save_snapshot(cache_dir: str, root: str, files: dict, recursive: bool) -> None:
    path = snapshot_path(cache_dir, root)
    storage.assert_outside(path, [root])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"root": root, "time": time.time(), "recursive": recursive, "files": files}, fh)
    os.replace(tmp, path)


def remember(lst: list, path: str, limit: int = MAX_RECENT) -> list:
    path = os.path.abspath(path)
    return ([path] + [p for p in lst if p != path])[:limit]
