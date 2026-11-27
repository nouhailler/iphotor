"""Recherche avancée multicritère : modèle de critères, champs disponibles, compilation SQL.

Les critères (JSON) sont compilés en une clause WHERE exécutée sur l'index LOCAL :
  - bibliothèque : table photos de index.sqlite3 (alias p) + metadata.sqlite3 attachée (md, alias m) ;
  - dossier : table photos en mémoire construite depuis la liste des images (mêmes noms de colonnes).
Aucun fichier source n'est relu.

Format :
  {"match": "all" | "any", "rules": [{"field": "make", "op": "contains", "value": "canon"}, …]}
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

from . import storage
from .metadata import RAW_EXTS, VIDEO_EXTS

LIBRARY, FOLDER = "library", "folder"

KEY_SQL = {
    LIBRARY: "COALESCE(NULLIF(p.guid, ''), 'id:' || p.id)",
    FOLDER: "p.key",
}

OPS = {
    "text": [("contains", "contient"), ("not_contains", "ne contient pas"), ("equals", "est"),
             ("starts", "commence par"), ("empty", "est vide"), ("not_empty", "n'est pas vide")],
    "number": [("eq", "="), ("ne", "≠"), ("lt", "<"), ("le", "≤"), ("gt", ">"), ("ge", "≥"),
               ("between", "entre"), ("known", "est connu"), ("unknown", "est inconnu")],
    "date": [("on", "le"), ("before", "avant le"), ("after", "après le"), ("between", "entre"),
             ("year", "année"), ("month", "mois (1-12)"), ("day", "jour du mois"), ("unknown", "inconnue")],
    "bool": [("yes", "oui"), ("no", "non")],
    "choice": [("is", "est"), ("is_not", "n'est pas")],
    "tag": [("has", "a le tag"), ("has_not", "n'a pas le tag"), ("any", "a au moins un tag"),
            ("none", "n'a aucun tag")],
}
NO_VALUE_OPS = {"empty", "not_empty", "known", "unknown", "yes", "no", "any", "none"}


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    group: str
    type: str
    library: Optional[str]          # expression SQL (None = indisponible)
    folder: Optional[str]
    meta: bool = False              # nécessite l'index des métadonnées
    choices: tuple = ()
    unit: str = ""
    scale: float = 1.0              # valeur saisie × scale = valeur stockée

    def expr(self, dialect: str) -> Optional[str]:
        return self.library if dialect == LIBRARY else self.folder


_DATE = "datetime(p.date + 978307200, 'unixepoch', 'localtime')"
_TAKEN = "datetime(m.taken, 'unixepoch', 'localtime')"

FIELDS: list[Field] = [
    # Informations générales
    Field("filename", "Nom du fichier", "Général", "text", "p.filename", "p.filename"),
    Field("ext", "Extension", "Général", "text", "iph_ext(p.filename)", "iph_ext(p.filename)"),
    Field("media", "Type de média", "Général", "choice",
          "CASE WHEN lower(p.media_type) = 'movie' OR iph_isvideo(p.filename) THEN 'video' ELSE 'photo' END",
          "CASE WHEN iph_isvideo(p.filename) THEN 'video' ELSE 'photo' END",
          choices=(("photo", "Photo"), ("video", "Vidéo"))),
    Field("caption", "Légende", "Général", "text", "p.caption", None),
    Field("comment", "Commentaire", "Général", "text", "p.comment", None),
    Field("event", "Événement", "Général", "text", "p.event_name", None),
    Field("album", "Album (nom)", "Général", "text", "@album", None),
    Field("folder", "Sous-dossier", "Général", "text", None, "p.folder"),
    Field("tag", "Tag", "Général", "tag", "@tag", "@tag"),
    Field("favorite", "Favori", "Général", "bool", "@fav", "@fav"),
    Field("rating", "Note (étoiles)", "Général", "number", "p.rating", None),
    Field("keyword", "Mot-clé Apple", "Général", "text", "@term:keyword", None),
    Field("person", "Personne (Apple)", "Général", "text", "@term:person", None),
    Field("place", "Lieu (Apple)", "Général", "text", "@term:place", None),
    # Dates
    Field("date", "Date", "Dates", "date", "p.date", "p.date"),
    Field("taken", "Date de prise de vue (EXIF)", "Dates", "date", "m.taken", "m.taken", meta=True),
    # Caractéristiques photographiques
    Field("width", "Largeur", "Photo", "number", "m.width", "m.width", meta=True, unit="px"),
    Field("height", "Hauteur", "Photo", "number", "m.height", "m.height", meta=True, unit="px"),
    Field("megapixels", "Résolution", "Photo", "number", "(m.width * m.height / 1e6)",
          "(m.width * m.height / 1e6)", meta=True, unit="Mpx"),
    Field("orientation", "Orientation", "Photo", "choice",
          "CASE WHEN m.width IS NULL THEN NULL WHEN m.width > m.height THEN 'landscape' "
          "WHEN m.width < m.height THEN 'portrait' ELSE 'square' END",
          "CASE WHEN m.width IS NULL THEN NULL WHEN m.width > m.height THEN 'landscape' "
          "WHEN m.width < m.height THEN 'portrait' ELSE 'square' END", meta=True,
          choices=(("landscape", "Paysage"), ("portrait", "Portrait"), ("square", "Carrée"))),
    Field("ratio", "Ratio largeur / hauteur", "Photo", "number", "(1.0 * m.width / NULLIF(m.height, 0))",
          "(1.0 * m.width / NULLIF(m.height, 0))", meta=True),
    Field("file_size", "Taille du fichier", "Photo", "number", "m.file_size", "COALESCE(m.file_size, p.size)",
          meta=True, unit="Mo", scale=1e6),
    Field("make", "Fabricant", "Photo", "text", "m.make", "m.make", meta=True),
    Field("model", "Appareil (modèle)", "Photo", "text", "m.model", "m.model", meta=True),
    Field("lens", "Objectif", "Photo", "text", "m.lens", "m.lens", meta=True),
    Field("focal", "Focale", "Photo", "number", "m.focal", "m.focal", meta=True, unit="mm"),
    Field("focal35", "Focale équiv. 24×36", "Photo", "number", "m.focal35", "m.focal35", meta=True, unit="mm"),
    Field("aperture", "Ouverture (f/)", "Photo", "number", "m.aperture", "m.aperture", meta=True),
    Field("exposure", "Temps de pose", "Photo", "number", "m.exposure", "m.exposure", meta=True, unit="s"),
    Field("iso", "Sensibilité ISO", "Photo", "number", "m.iso", "m.iso", meta=True),
    # Autres
    Field("gps", "Coordonnées GPS", "Autres", "bool", "@gps", "@gps", meta=True),
    Field("raw", "Fichier RAW", "Autres", "bool", "iph_israw(p.filename)", "iph_israw(p.filename)"),
    Field("has_exif", "Métadonnées EXIF présentes", "Autres", "bool", "@exif", "@exif", meta=True),
]
FIELD_BY_NAME = {f.name: f for f in FIELDS}


class AnnotationSource(Protocol):
    def favorites(self) -> set[str]: ...
    def keys_with_tag(self, tag: str) -> set[str]: ...
    def tagged_keys(self) -> set[str]: ...


def install_functions(con: sqlite3.Connection) -> None:
    """Fonctions SQL utilisées par les critères (déterministes, sans accès disque)."""
    con.create_function("iph_norm", 1, storage.norm, deterministic=True)
    con.create_function("iph_ext", 1, lambda f: os.path.splitext(f or "")[1].lower().lstrip("."),
                        deterministic=True)
    con.create_function("iph_israw", 1, lambda f: int(os.path.splitext(f or "")[1].lower() in RAW_EXTS),
                        deterministic=True)
    con.create_function("iph_isvideo", 1, lambda f: int(os.path.splitext(f or "")[1].lower() in VIDEO_EXTS),
                        deterministic=True)


def parse_number(text) -> Optional[float]:
    """« 2,8 », « f/2.8 », « 1/125 », « 400 » → nombre."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower().replace(",", ".").replace(" ", "")
    s = re.sub(r"^f/?", "", s)
    s = re.sub(r"(mm|px|s|mo|mpx|iso)$", "", s)
    if "/" in s:
        a, _, b = s.partition("/")
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(text) -> Optional[_dt.date]:
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _day_bounds(d: _dt.date, apple: bool) -> tuple[float, float]:
    start = _dt.datetime(d.year, d.month, d.day).timestamp()
    end = start + 86400
    if apple:
        start, end = storage.unix_to_apple(start), storage.unix_to_apple(end)
    return start, end


@dataclass
class Compiled:
    where: str
    params: list
    warnings: list
    uses_meta: bool


class CriteriaError(ValueError):
    pass


def compile_criteria(criteria: dict, dialect: str, annotations: Optional[AnnotationSource] = None) -> Compiled:
    rules = [r for r in criteria.get("rules", []) if r.get("field")]
    match = criteria.get("match", "all")
    parts, params, warnings = [], [], []
    uses_meta = False
    key = KEY_SQL[dialect]
    for rule in rules:
        f = FIELD_BY_NAME.get(rule["field"])
        if f is None:
            raise CriteriaError(f"Champ inconnu : {rule['field']}")
        expr = f.expr(dialect)
        op = rule.get("op") or OPS[f.type][0][0]
        if op not in {o for o, _ in OPS[f.type]}:
            raise CriteriaError(f"Opérateur « {op} » invalide pour « {f.label} »")
        if expr is None:
            warnings.append(f"« {f.label} » n'est pas disponible pour cette source : critère toujours faux.")
            parts.append("0")
            continue
        uses_meta = uses_meta or f.meta
        v, v2 = rule.get("value"), rule.get("value2")
        if op not in NO_VALUE_OPS and (v is None or str(v).strip() == "") and f.type != "bool":
            raise CriteriaError(f"Valeur manquante pour « {f.label} ».")
        sql, ps = _rule_sql(f, expr, op, v, v2, key, annotations)
        parts.append(f"({sql})")
        params += ps
    if not parts:
        return Compiled("1", [], warnings, False)
    joiner = " OR " if match == "any" else " AND "
    return Compiled(joiner.join(parts), params, warnings, uses_meta)


def _rule_sql(f: Field, expr: str, op: str, v, v2, key: str,
              annotations: Optional[AnnotationSource]) -> tuple[str, list]:
    t = f.type
    if expr == "@fav":
        keys = json.dumps(sorted(annotations.favorites())) if annotations else "[]"
        inside = f"{key} IN (SELECT value FROM json_each(?))"
        return (inside if op == "yes" else f"NOT {inside}"), [keys]
    if expr == "@tag":
        if annotations is None:
            return "0", []
        if op in ("any", "none"):
            keys = json.dumps(sorted(annotations.tagged_keys()))
        else:
            keys = json.dumps(sorted(annotations.keys_with_tag(str(v).strip())))
        inside = f"{key} IN (SELECT value FROM json_each(?))"
        return (inside if op in ("has", "any") else f"NOT {inside}"), [keys]
    if expr == "@gps":
        return ("m.gps_lat IS NOT NULL" if op == "yes" else "m.status = 'ok' AND m.gps_lat IS NULL"), []
    if expr == "@exif":
        return ("m.has_exif = 1" if op == "yes" else "m.status = 'ok' AND COALESCE(m.has_exif, 0) = 0"), []
    if expr == "@album":
        sub = ("p.id IN (SELECT ap.photo_id FROM album_photos ap JOIN albums a ON a.id = ap.album_id "
               "WHERE a.kind != 'event' AND iph_norm(a.name) {cond})")
        return _text_cond(sub, op, v, wrap=False)
    if expr.startswith("@term:"):
        kind = expr[6:]
        sub = f"{key} IN (SELECT t.key FROM md.apple_terms t WHERE t.kind = '{kind}' AND t.value_norm {{cond}})"
        return _text_cond(sub, op, v, wrap=False)
    if t == "text":
        return _text_cond(expr, op, v)
    if t == "bool":
        return (f"COALESCE({expr}, 0) = 1" if op == "yes" else f"COALESCE({expr}, 0) = 0"), []
    if t == "choice":
        return (f"{expr} = ?" if op == "is" else f"({expr} IS NULL OR {expr} != ?)"), [str(v)]
    if t == "number":
        if op == "known":
            return f"{expr} IS NOT NULL", []
        if op == "unknown":
            return f"{expr} IS NULL", []
        a = parse_number(v)
        if a is None:
            raise CriteriaError(f"Nombre invalide pour « {f.label} » : {v}")
        a *= f.scale
        if op == "between":
            b = parse_number(v2)
            if b is None:
                raise CriteriaError(f"Seconde valeur invalide pour « {f.label} » : {v2}")
            b *= f.scale
            lo, hi = min(a, b), max(a, b)
            eps = 1e-9 * max(1.0, abs(hi))
            return f"{expr} BETWEEN ? AND ?", [lo - eps, hi + eps]
        sym = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
        if op in ("eq", "ne"):   # tolérance (valeurs EXIF rationnelles, Mo arrondis)
            eps = max(1e-6, abs(a) * 0.005)
            cond = f"{expr} BETWEEN ? AND ?" if op == "eq" else f"({expr} NOT BETWEEN ? AND ?)"
            return cond, [a - eps, a + eps]
        return f"{expr} {sym} ?", [a]
    if t == "date":
        apple = expr == "p.date"
        dt_expr = _DATE if apple else _TAKEN
        if op == "unknown":
            return f"{expr} IS NULL", []
        if op in ("year", "month", "day"):
            n = parse_number(v)
            if n is None:
                raise CriteriaError(f"Valeur invalide pour « {f.label} » : {v}")
            fmt = {"year": "%Y", "month": "%m", "day": "%d"}[op]
            return f"CAST(strftime('{fmt}', {dt_expr}) AS INTEGER) = ?", [int(n)]
        d1 = parse_date(v)
        if d1 is None:
            raise CriteriaError(f"Date invalide pour « {f.label} » : {v} (format AAAA-MM-JJ ou JJ/MM/AAAA)")
        s1, e1 = _day_bounds(d1, apple)
        if op == "on":
            return f"{expr} >= ? AND {expr} < ?", [s1, e1]
        if op == "before":
            return f"{expr} < ?", [s1]
        if op == "after":
            return f"{expr} >= ?", [e1]
        d2 = parse_date(v2)
        if d2 is None:
            raise CriteriaError(f"Seconde date invalide pour « {f.label} » : {v2}")
        s2, e2 = _day_bounds(d2, apple)
        return f"{expr} >= ? AND {expr} < ?", [min(s1, s2), max(e1, e2)]
    raise CriteriaError(f"Type de champ non géré : {t}")


def _text_cond(expr: str, op: str, v, wrap: bool = True) -> tuple[str, list]:
    if op in ("empty", "not_empty"):
        if not wrap:      # sous-requête : « vide » = aucun élément
            cond = expr.replace("{cond}", "!= ''")
            return (f"NOT {cond}" if op == "empty" else cond), []
        return (f"COALESCE({expr}, '') = ''" if op == "empty" else f"COALESCE({expr}, '') != ''"), []
    q = storage.norm(str(v).strip())
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if op in ("contains", "not_contains"):
        pat, cmp = f"%{esc}%", "LIKE ? ESCAPE '\\'"
    elif op == "starts":
        pat, cmp = f"{esc}%", "LIKE ? ESCAPE '\\'"
    else:
        pat, cmp = q, "= ?"
    if not wrap:
        cond = expr.replace("{cond}", cmp)
        return (f"NOT {cond}" if op == "not_contains" else cond), [pat]
    base = f"iph_norm({expr}) {cmp}"
    if op == "not_contains":
        return f"NOT ({base})", [pat]
    return base, [pat]


def build_query(compiled: Compiled, dialect: str, with_meta: bool) -> str:
    key = KEY_SQL[dialect]
    join = f" LEFT JOIN md.photo_meta m ON m.key = {key}" if with_meta else ""
    return f"SELECT p.id FROM photos p{join} WHERE {compiled.where} ORDER BY p.date, p.id"


def run(con: sqlite3.Connection, criteria: dict, dialect: str,
        annotations: Optional[AnnotationSource] = None, meta_attached: bool = True) -> tuple[list[int], list[str]]:
    """Exécute une recherche ; renvoie (ids des photos, avertissements)."""
    install_functions(con)
    c = compile_criteria(criteria, dialect, annotations)
    if c.uses_meta and not meta_attached:
        raise CriteriaError("L'index des métadonnées n'est pas disponible : lancez l'analyse des métadonnées.")
    ids = [r[0] for r in con.execute(build_query(c, dialect, meta_attached), c.params)]
    return ids, c.warnings


def attach_meta(con: sqlite3.Connection, meta_path: str) -> bool:
    """Attache metadata.sqlite3 (alias md) si elle existe ; sinon une base vide en mémoire."""
    try:
        con.execute("DETACH DATABASE md")
    except sqlite3.Error:
        pass
    if meta_path and os.path.isfile(meta_path):
        con.execute("ATTACH DATABASE ? AS md", (meta_path,))
        return True
    con.execute("ATTACH DATABASE ':memory:' AS md")
    from .metadata import SCHEMA
    con.executescript(SCHEMA.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE IF NOT EXISTS md.")
                      .replace("CREATE INDEX IF NOT EXISTS idx_", "CREATE INDEX IF NOT EXISTS md.idx_"))
    return True


def folder_table(con: sqlite3.Connection, photos: Iterable, key_of: Callable) -> None:
    """Construit la table photos (en mémoire) d'un dossier, compatible avec le dialecte FOLDER."""
    con.execute("DROP TABLE IF EXISTS photos")
    con.execute("CREATE TEMP TABLE photos (id INTEGER PRIMARY KEY, key TEXT, filename TEXT, folder TEXT, "
                "size INTEGER, date REAL, media_type TEXT)")
    con.executemany("INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(p.id, key_of(p), p.filename, p.folder, p.size, p.date, p.media_type) for p in photos])


def describe(criteria: dict) -> str:
    """Résumé lisible d'une recherche (titre de vue, liste des recherches enregistrées)."""
    rules = criteria.get("rules", [])
    if not rules:
        return "Toutes les photos"
    out = []
    for r in rules:
        f = FIELD_BY_NAME.get(r.get("field"))
        if f is None:
            continue
        op_label = dict(OPS[f.type]).get(r.get("op"), r.get("op"))
        val = r.get("value")
        if f.type == "choice":
            val = dict(f.choices).get(val, val)
        txt = f"{f.label} {op_label}"
        if r.get("op") not in NO_VALUE_OPS and val not in (None, ""):
            txt += f" {val}"
            if r.get("op") == "between":
                txt += f" et {r.get('value2')}"
        out.append(txt)
    return (" ou " if criteria.get("match") == "any" else " et ").join(out)
