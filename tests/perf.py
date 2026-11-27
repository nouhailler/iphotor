"""Mesures de performance sur des bibliothèques synthétiques (1 000, 10 000, 50 000 photos…).

Lancement : python3 tests/perf.py [tailles…]     ex. python3 tests/perf.py 1000 10000 50000
Les photos pointent vers un petit jeu d'images réelles (réutilisées) ; l'index des métadonnées des
grandes bibliothèques est rempli de valeurs synthétiques pour mesurer la recherche et les
statistiques, la lecture EXIF réelle étant mesurée sur 1 000 fichiers.
Résultats : tableau sur la sortie standard + tests/perf-resultats.json.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import plistlib
import random
import resource
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from helpers import write_jpeg  # noqa: E402

TMP = tempfile.mkdtemp(prefix="iphotor-perf-")
for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    os.environ[k] = os.path.join(TMP, k.lower())
spec = importlib.util.spec_from_file_location("iphotor_app", os.path.join(ROOT, "iphotor.py"))
ip = importlib.util.module_from_spec(spec)
sys.modules["iphotor_app"] = ip
spec.loader.exec_module(ip)
from iphotorlib import duplicates, metadata, search, stats, timeline  # noqa: E402

APPLE = dt.datetime(2001, 1, 1)


def make_library(n: int, pool: list[str]) -> str:
    lib = os.path.join(TMP, f"lib{n}", "iPhoto Library")
    os.makedirs(os.path.join(lib, "Masters"), exist_ok=True)
    rnd = random.Random(n)
    master, rolls, albums = {}, [], []
    per_roll = 40
    t0 = dt.datetime(2004, 1, 1)
    for i in range(1, n + 1):
        when = t0 + dt.timedelta(minutes=37 * i + rnd.randint(0, 30))
        master[str(i)] = {"GUID": f"G{i:07d}", "MediaType": "Image", "Caption": f"IMG_{i:05d}",
                          "Comment": "plage" if i % 50 == 0 else "", "Rating": i % 6 if i % 7 == 0 else 0,
                          "Roll": 1 + (i - 1) // per_roll, "ImagePath": pool[i % len(pool)], "OriginalPath": "",
                          "DateAsTimerInterval": (when - APPLE).total_seconds()}
    keys = list(master)
    for r in range(0, (n + per_roll - 1) // per_roll):
        kl = keys[r * per_roll:(r + 1) * per_roll]
        rolls.append({"RollID": r + 1, "RollName": f"Événement {r + 1}", "KeyList": kl,
                      "RollDateAsTimerInterval": master[kl[0]]["DateAsTimerInterval"], "ProjectUuid": f"R{r}"})
        albums.append({"AlbumId": 100000 + r, "AlbumName": f"Événement {r + 1}", "Album Type": "Event",
                       "GUID": f"R{r}", "KeyList": kl})
    for a in range(30):
        albums.append({"AlbumId": a + 1, "AlbumName": f"Album {a}", "Album Type": "Regular",
                       "KeyList": rnd.sample(keys, min(len(keys), 300))})
    with open(os.path.join(lib, "AlbumData.xml"), "wb") as fh:
        plistlib.dump({"Archive Path": lib, "Master Image List": master, "List of Rolls": rolls,
                       "List of Albums": albums}, fh)
    return lib


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def timed(fn, *a, repeat: int = 1, **kw):
    best = None
    out = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn(*a, **kw)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best, out


def bench(n: int, pool: list[str]) -> dict:
    res = {"photos": n}
    lib_path = make_library(n, pool)
    res["xml_mo"] = round(os.path.getsize(os.path.join(lib_path, "AlbumData.xml")) / 1e6, 1)
    lib = ip.LibraryManager(lib_path)
    cache = ip.CacheManager(lib)
    cache.ensure_dirs()
    res["index_s"], _ = timed(cache.build)
    con = cache.connect()
    pm, am = ip.PhotoManager(con), ip.AlbumManager(con)
    res["page_s"], _ = timed(lambda: pm.page(ip.View("all"), (n // 2 // 100) * 100, 100), repeat=3)
    res["count_s"], _ = timed(lambda: pm.count(ip.View("all")), repeat=3)
    res["search_s"], found = timed(lambda: pm.count(ip.View("search", query="plage")), repeat=3)
    res["events_tree_s"], _ = timed(am.events_by_year, repeat=3)
    # index des métadonnées synthétique (valeurs plausibles) pour la recherche avancée
    md = metadata.MetadataIndex(os.path.join(cache.cache_dir, "metadata.sqlite3"))
    mcon = md.connect()
    rnd = random.Random(1)
    rows = []
    for i in range(1, n + 1):
        rows.append(metadata.MetadataIndex._row({
            "status": "ok", "path": pool[i % len(pool)], "file_size": rnd.randint(1, 8) * 1_000_000,
            "width": 4000, "height": 3000 if i % 4 else 4500, "has_exif": 1,
            "make": "Canon" if i % 3 else "NIKON CORPORATION", "model": "Canon EOS 450D" if i % 3 else "NIKON D7000",
            "lens": "EF50mm", "iso": rnd.choice([100, 200, 400, 800, 1600]), "focal": float(rnd.choice([18, 35, 50, 85])),
            "aperture": 2.8, "exposure": 1 / 125, "taken": None,
            "gps_lat": 45 + rnd.random() if i % 5 == 0 else None, "gps_lon": 6 + rnd.random() if i % 5 == 0 else None},
            f"G{i:07d}", i))
    metadata.MetadataIndex._write(mcon, rows)
    mcon.close()
    search.install_functions(con)
    search.attach_meta(con, md.path)
    crit = {"match": "all", "rules": [
        {"field": "date", "op": "between", "value": "2005-01-01", "value2": "2008-12-31"},
        {"field": "model", "op": "contains", "value": "d7000"},
        {"field": "iso", "op": "ge", "value": "400"}, {"field": "orientation", "op": "is", "value": "landscape"}]}

    class A:
        def favorites(self):
            return {f"G{i:07d}" for i in range(1, n, 11)}

        def keys_with_tag(self, t):
            return {f"G{i:07d}" for i in range(1, n, 17)}

        def tagged_keys(self):
            return self.keys_with_tag("")
    crit2 = {"match": "all", "rules": crit["rules"] + [{"field": "favorite", "op": "yes"},
                                                       {"field": "tag", "op": "has", "value": "x"}]}
    res["adv_search_s"], (ids, _w) = timed(lambda: search.run(con, crit, search.LIBRARY, A()), repeat=3)
    res["adv_search_hits"] = len(ids)
    res["adv_search5_s"], (ids2, _w) = timed(lambda: search.run(con, crit2, search.LIBRARY, A()), repeat=3)
    items = [(r[0], r[1] + 978307200) for r in con.execute("SELECT id, date FROM photos")]
    res["timeline_s"], t = timed(lambda: timeline.Timeline.build(items), repeat=3)
    recs = [{"filename": f"IMG_{i}.JPG", "date": d, **{k: v for k, v in zip(metadata.META_COLUMNS, row)}}
            for (i, d), row in zip(items, rows)]
    res["stats_s"], _ = timed(lambda: stats.compute(recs), repeat=2)
    # regroupement des doublons (empreintes synthétiques, 2 % de quasi-doublons)
    ents = []
    for i in range(n):
        h = rnd.getrandbits(64)
        ents.append(duplicates.Entry(key=str(i), path=str(i), sha256=f"{i:064x}", phash=f"{h:016x}"))
        if i % 50 == 0:
            ents.append(duplicates.Entry(key=f"d{i}", path=f"d{i}", sha256=f"d{i:063x}",
                                         phash=f"{h ^ (1 << rnd.randint(0, 63)):016x}"))
    res["dup_group_s"], rep = timed(lambda: duplicates.find_groups(ents))
    res["dup_groups"] = len(rep.groups)
    con.close()
    res["rss_mo"] = round(rss_mb())
    shutil.rmtree(os.path.dirname(lib_path), ignore_errors=True)
    shutil.rmtree(cache.cache_dir, ignore_errors=True)
    return res


def bench_files(pool: list[str]) -> dict:
    """Coûts réels par fichier : lecture EXIF (en-tête) et empreintes SHA-256 + pHash."""
    out = {"fichiers": len(pool)}
    t = time.perf_counter()
    for p in pool:
        metadata.extract(p)
    out["exif_ms_par_fichier"] = round((time.perf_counter() - t) / len(pool) * 1000, 2)
    t = time.perf_counter()
    for p in pool:
        duplicates.sha256_file(p)
    out["sha256_ms_par_fichier"] = round((time.perf_counter() - t) / len(pool) * 1000, 2)
    t = time.perf_counter()
    for p in pool[:300]:
        duplicates.phash_file(p)
    out["phash_ms_par_fichier"] = round((time.perf_counter() - t) / min(300, len(pool)) * 1000, 2)
    out["taille_moyenne_ko"] = round(sum(os.path.getsize(p) for p in pool) / len(pool) / 1024)
    return out


def main():
    sizes = [int(a) for a in sys.argv[1:]] or [1000, 10000, 50000]
    pool_dir = os.path.join(TMP, "pool")
    pool = [write_jpeg(os.path.join(pool_dir, f"p{i:04d}.jpg"), seed=i, size=(1600, 1067)) for i in range(1000)]
    results = {"machine": os.uname().machine, "python": sys.version.split()[0], "fichiers": bench_files(pool),
               "bibliotheques": []}
    for n in sizes:
        r = bench(n, pool)
        results["bibliotheques"].append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    print(json.dumps(results["fichiers"], ensure_ascii=False))
    with open(os.path.join(HERE, "perf-resultats.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
