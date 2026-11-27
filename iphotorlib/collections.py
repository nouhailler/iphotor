"""Albums virtuels, albums intelligents et recherches enregistrées.

Stockés dans ~/.local/share/iphotor/collections.sqlite3 (données de l'utilisateur, schéma
versionné, sauvegarde avant migration). Un album virtuel ne contient que des RÉFÉRENCES :
le supprimer ne touche à aucune photo.

Une référence mémorise : la portée (bibliothèque « bib:<racine> » ou « fichiers »), la clé
(GUID / « id:N » ou chemin), le chemin absolu du fichier au moment de l'ajout, le nom, la
taille et la date. Cela permet de l'afficher sans la source d'origine et de la retrouver si
le disque ou la bibliothèque a changé d'emplacement.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from . import storage

DB_NAME = "collections.sqlite3"

MIGRATIONS = [
    # v0 -> v1
    """
    CREATE TABLE albums (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'manual',      -- manual | smart
        criteria TEXT,                            -- JSON (albums intelligents)
        created REAL, modified REAL, position INTEGER DEFAULT 0
    );
    CREATE TABLE items (
        album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        scope TEXT NOT NULL, key TEXT NOT NULL,
        path TEXT, filename TEXT, size INTEGER, date REAL, added REAL,
        PRIMARY KEY (album_id, scope, key)
    );
    CREATE INDEX idx_items_album ON items(album_id, position);
    CREATE TABLE searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, criteria TEXT NOT NULL, created REAL
    );
    """,
]


@dataclass
class VirtualAlbum:
    id: int
    name: str
    kind: str
    criteria: Optional[dict]
    count: int = 0
    created: float = 0.0
    modified: float = 0.0


@dataclass
class ItemRef:
    album_id: int
    position: int
    scope: str
    key: str
    path: str
    filename: str
    size: int
    date: Optional[float]

    @property
    def exists(self) -> bool:
        try:
            return bool(self.path) and os.path.isfile(self.path)
        except (OSError, ValueError):
            return False


class Collections:
    def __init__(self, path: Optional[str] = None, protected: Callable[[], Iterable[str]] = lambda: ()):
        self.path = path or os.path.join(storage.data_root(), DB_NAME)
        self.protected = protected
        self._con: Optional[sqlite3.Connection] = None

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = storage.open_user_db(self.path, MIGRATIONS, protected=self.protected())
            self._con.execute("PRAGMA foreign_keys = ON")
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # -- albums ---------------------------------------------------------------------
    def albums(self, kind: Optional[str] = None) -> list[VirtualAlbum]:
        sql = ("SELECT a.*, (SELECT COUNT(*) FROM items i WHERE i.album_id = a.id) AS n FROM albums a "
               + ("WHERE a.kind = ? " if kind else "") + "ORDER BY a.position, lower(a.name), a.id")
        rows = self.con.execute(sql, (kind,) if kind else ())
        return [VirtualAlbum(r["id"], r["name"], r["kind"], json.loads(r["criteria"]) if r["criteria"] else None,
                             r["n"], r["created"] or 0, r["modified"] or 0) for r in rows]

    def get(self, album_id: int) -> Optional[VirtualAlbum]:
        return next((a for a in self.albums() if a.id == album_id), None)

    def create(self, name: str, kind: str = "manual", criteria: Optional[dict] = None) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Le nom de l'album est vide.")
        now = time.time()
        cur = self.con.execute("INSERT INTO albums (name, kind, criteria, created, modified, position) "
                               "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM albums))",
                               (name, kind, json.dumps(criteria) if criteria is not None else None, now, now))
        self.con.commit()
        return cur.lastrowid

    def rename(self, album_id: int, name: str) -> None:
        if not name.strip():
            raise ValueError("Le nom de l'album est vide.")
        self.con.execute("UPDATE albums SET name = ?, modified = ? WHERE id = ?", (name.strip(), time.time(), album_id))
        self.con.commit()

    def set_criteria(self, album_id: int, criteria: dict) -> None:
        self.con.execute("UPDATE albums SET criteria = ?, modified = ? WHERE id = ?",
                         (json.dumps(criteria), time.time(), album_id))
        self.con.commit()

    def delete(self, album_id: int) -> None:
        """Supprime l'album et ses références — jamais les photos."""
        self.con.execute("DELETE FROM items WHERE album_id = ?", (album_id,))
        self.con.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        self.con.commit()

    # -- références -------------------------------------------------------------------
    def items(self, album_id: int) -> list[ItemRef]:
        rows = self.con.execute("SELECT * FROM items WHERE album_id = ? ORDER BY position", (album_id,))
        return [ItemRef(r["album_id"], r["position"], r["scope"], r["key"], r["path"] or "",
                        r["filename"] or "", r["size"] or 0, r["date"]) for r in rows]

    def add(self, album_id: int, refs: Iterable[dict]) -> int:
        """refs : dicts avec scope, key, path, filename, size, date. Les doublons sont ignorés."""
        start = self.con.execute("SELECT COALESCE(MAX(position), 0) FROM items WHERE album_id = ?",
                                 (album_id,)).fetchone()[0]
        n = 0
        now = time.time()
        for r in refs:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO items (album_id, position, scope, key, path, filename, size, date, added) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (album_id, start + n + 1, r["scope"], r["key"], r.get("path", ""), r.get("filename", ""),
                 int(r.get("size") or 0), r.get("date"), now))
            n += cur.rowcount
        self.con.execute("UPDATE albums SET modified = ? WHERE id = ?", (now, album_id))
        self.con.commit()
        return n

    def remove(self, album_id: int, refs: Iterable[tuple[str, str]]) -> int:
        n = 0
        for scope, key in refs:
            n += self.con.execute("DELETE FROM items WHERE album_id = ? AND scope = ? AND key = ?",
                                  (album_id, scope, key)).rowcount
        self._renumber(album_id)
        return n

    def move(self, album_id: int, refs: list[tuple[str, str]], delta: int) -> None:
        """Déplace des éléments de `delta` positions (réorganisation)."""
        items = self.items(album_id)
        order = [(i.scope, i.key) for i in items]
        chosen = [r for r in order if r in set(refs)]
        if not chosen:
            return
        rest = [r for r in order if r not in set(refs)]
        first = order.index(chosen[0])
        new_pos = max(0, min(len(rest), first + delta))
        order = rest[:new_pos] + chosen + rest[new_pos:]
        self._write_order(album_id, order)

    def _renumber(self, album_id: int) -> None:
        self._write_order(album_id, [(i.scope, i.key) for i in self.items(album_id)])

    def _write_order(self, album_id: int, order: list[tuple[str, str]]) -> None:
        # deux passes pour éviter les collisions de clés primaires temporaires
        self.con.execute("UPDATE items SET position = -position - 1 WHERE album_id = ?", (album_id,))
        for i, (scope, key) in enumerate(order, start=1):
            self.con.execute("UPDATE items SET position = ? WHERE album_id = ? AND scope = ? AND key = ?",
                             (i, album_id, scope, key))
        self.con.execute("UPDATE albums SET modified = ? WHERE id = ?", (time.time(), album_id))
        self.con.commit()

    def relocate(self, album_id: Optional[int], old_prefix: str, new_prefix: str) -> int:
        """Remplace un préfixe de chemin (disque ou bibliothèque déplacés). Ne touche qu'aux références."""
        old_prefix, new_prefix = old_prefix.rstrip("/"), new_prefix.rstrip("/")
        n = 0
        rows = self.con.execute("SELECT album_id, scope, key, path FROM items" +
                                (" WHERE album_id = ?" if album_id is not None else ""),
                                (album_id,) if album_id is not None else ()).fetchall()
        for r in rows:
            p = r["path"] or ""
            if p == old_prefix or p.startswith(old_prefix + "/"):
                newp = new_prefix + p[len(old_prefix):]
                if os.path.isfile(newp):
                    key = r["key"]
                    if r["scope"] == "fichiers":
                        key = newp
                    self.con.execute("UPDATE OR IGNORE items SET path = ?, key = ? WHERE album_id = ? AND scope = ? "
                                     "AND key = ?", (newp, key, r["album_id"], r["scope"], r["key"]))
                    n += 1
        self.con.commit()
        return n

    def update_path(self, album_id: int, scope: str, key: str, new_path: str) -> None:
        new_key = new_path if scope == "fichiers" else key
        self.con.execute("UPDATE OR IGNORE items SET path = ?, key = ? WHERE album_id = ? AND scope = ? AND key = ?",
                         (new_path, new_key, album_id, scope, key))
        self.con.commit()

    def find_by_name(self, album_id: int, folder: str, recursive: bool = True,
                     cancelled: Callable[[], bool] = lambda: False) -> int:
        """Retrouve les références manquantes par nom ET taille dans un dossier choisi (lecture seule)."""
        missing = [i for i in self.items(album_id) if not i.exists and i.filename]
        if not missing:
            return 0
        wanted: dict[tuple[str, int], list[ItemRef]] = {}
        for i in missing:
            wanted.setdefault((i.filename, i.size), []).append(i)
        found = 0
        for dirpath, dirnames, filenames in os.walk(folder):
            if cancelled():
                break
            dirnames[:] = [d for d in dirnames if not d.startswith(".")] if recursive else []
            for name in filenames:
                cands = [k for k in wanted if k[0] == name]
                if not cands:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                for ref in wanted.pop((name, size), []):
                    self.update_path(album_id, ref.scope, ref.key, full)
                    found += 1
        return found

    # -- recherches enregistrées -------------------------------------------------------
    def searches(self) -> list[tuple[int, str, dict]]:
        return [(r["id"], r["name"], json.loads(r["criteria"]))
                for r in self.con.execute("SELECT * FROM searches ORDER BY lower(name), id")]

    def save_search(self, name: str, criteria: dict) -> int:
        if not name.strip():
            raise ValueError("Le nom de la recherche est vide.")
        cur = self.con.execute("INSERT INTO searches (name, criteria, created) VALUES (?, ?, ?)",
                               (name.strip(), json.dumps(criteria), time.time()))
        self.con.commit()
        return cur.lastrowid

    def delete_search(self, search_id: int) -> None:
        self.con.execute("DELETE FROM searches WHERE id = ?", (search_id,))
        self.con.commit()

    # -- contrôle ------------------------------------------------------------------------
    def missing_refs(self) -> list[tuple[str, ItemRef]]:
        out = []
        for a in self.albums("manual"):
            out += [(a.name, i) for i in self.items(a.id) if not i.exists]
        return out
