"""Stockage local d'iphotor : emplacements, bases SQLite contrôlées, garde-fous d'écriture."""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
import unicodedata
import urllib.parse
from typing import Callable, Iterable, Optional

APPLE_EPOCH_OFFSET = 978307200.0      # 2001-01-01 (Apple) -> 1970-01-01 (Unix)


def cache_root() -> str:
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                        "photoexplorer_iphoto")


def data_root() -> str:
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "iphotor")


def is_inside(path: str, directory: str) -> bool:
    """path est-il dans directory (ou égal) ? Chemins résolus (liens symboliques compris)."""
    if not path or not directory:
        return False
    try:
        p = os.path.realpath(path)
        d = os.path.realpath(directory)
    except (OSError, ValueError):
        return False
    return p == d or p.startswith(d.rstrip(os.sep) + os.sep)


def assert_outside(path: str, protected: Iterable[str]) -> None:
    """Garde-fou commun : refuse toute écriture dans une source protégée."""
    for root in protected:
        if root and is_inside(path, root):
            raise PermissionError(f"Écriture refusée : {path} se trouve dans une source en lecture seule ({root}).")


def norm(text) -> str:
    """Texte normalisé pour la recherche : minuscules, sans accents."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def source_ro_uri(path: str) -> str:
    """URI SQLite en lecture seule stricte (aucun verrou, aucun fichier -wal/-shm créé)."""
    return "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro&immutable=1"


def open_source_db(path: str) -> sqlite3.Connection:
    """Ouvre une base SOURCE en lecture seule stricte. À réserver aux bases sans journal WAL
    pendant ; sinon utiliser copy_source_db() pour lire aussi les données du journal."""
    con = sqlite3.connect(source_ro_uri(path), uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


class SourceDBCopy:
    """Copie temporaire d'une base source (+ journal WAL) dans le cache, ouverte puis supprimée.

    La base d'origine n'est que lue (shutil.copyfile) ; la copie est vérifiée hors des sources.
    """

    def __init__(self, src: str, workdir: str, protected: Iterable[str]):
        self.src = src
        self.protected = list(protected)
        self.dir = os.path.join(workdir, f"srcdb-{os.getpid()}-{int(time.time() * 1000)}.tmp")
        self.path = os.path.join(self.dir, os.path.basename(src))
        self.con: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        assert_outside(self.dir, self.protected)
        os.makedirs(self.dir, exist_ok=True)
        shutil.copyfile(self.src, self.path)
        for ext in ("-wal", "-shm"):
            if os.path.isfile(self.src + ext):
                shutil.copyfile(self.src + ext, self.path + ext)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        return self.con

    def __exit__(self, *exc) -> None:
        try:
            if self.con is not None:
                self.con.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


def tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def open_user_db(path: str, migrations: list[str],
                 protected: Iterable[str] = (), backup: bool = True) -> sqlite3.Connection:
    """Ouvre (ou crée) une base de données propre à iphotor et applique les migrations.

    migrations[i] fait passer le schéma de la version i à i + 1 (PRAGMA user_version).
    Avant de migrer une base existante, une sauvegarde « <base>.bak-v<N> » est créée.
    """
    assert_outside(path, protected)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existed = os.path.isfile(path) and os.path.getsize(path) > 0
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > len(migrations):
        con.close()
        raise RuntimeError(f"{os.path.basename(path)} a été créée par une version plus récente d'iphotor "
                           f"(schéma {version}, attendu ≤ {len(migrations)}).")
    if version < len(migrations):
        if existed and backup and version > 0:
            con.close()
            shutil.copy2(path, f"{path}.bak-v{version}")
            con = sqlite3.connect(path, timeout=10)
            con.row_factory = sqlite3.Row
        for v in range(version, len(migrations)):
            con.executescript(migrations[v])
            con.execute(f"PRAGMA user_version = {v + 1}")
        con.commit()
    return con


def file_signature(path: str) -> Optional[str]:
    """Signature légère d'un fichier (taille + date) : détecte les changements sans le lire."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return f"{st.st_size}:{int(st.st_mtime)}"


def apple_to_unix(value: Optional[float]) -> Optional[float]:
    return None if value is None else value + APPLE_EPOCH_OFFSET


def unix_to_apple(value: Optional[float]) -> Optional[float]:
    return None if value is None else value - APPLE_EPOCH_OFFSET


def run_batches(items: list, size: int, fn: Callable[[list], None],
                cancelled: Callable[[], bool] = lambda: False) -> bool:
    """Applique fn par lots ; renvoie False si annulé en cours de route."""
    for i in range(0, len(items), size):
        if cancelled():
            return False
        fn(items[i:i + size])
    return True
