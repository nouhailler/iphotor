"""Tests unitaires du paquet iphotorlib (sans interface graphique)."""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from helpers import ROOT, diff, make_exif, scene, snapshot, write_jpeg  # noqa: F401

from iphotorlib import (applemeta, collections, deps, duplicates, export, folders, geo, metadata,
                        search, stats, storage, timeline)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="iphotor-test-")
        self.src = os.path.join(self.tmp, "source")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.src)
        os.makedirs(self.cache)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class StorageTests(TmpCase):
    def test_assert_outside(self):
        with self.assertRaises(PermissionError):
            storage.assert_outside(os.path.join(self.src, "x.db"), [self.src])
        storage.assert_outside(os.path.join(self.cache, "x.db"), [self.src])
        # lien symbolique vers la source : refusé aussi
        link = os.path.join(self.tmp, "lien")
        os.symlink(self.src, link)
        with self.assertRaises(PermissionError):
            storage.assert_outside(os.path.join(link, "x.db"), [self.src])

    def test_user_db_migration_and_backup(self):
        path = os.path.join(self.cache, "u.sqlite3")
        con = storage.open_user_db(path, ["CREATE TABLE a (x);"])
        con.execute("INSERT INTO a VALUES (1)")
        con.commit()
        con.close()
        con = storage.open_user_db(path, ["CREATE TABLE a (x);", "ALTER TABLE a ADD COLUMN y;"])
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(con.execute("SELECT x FROM a").fetchone()[0], 1)
        con.close()
        self.assertTrue(os.path.isfile(path + ".bak-v1"))
        with self.assertRaises(RuntimeError):     # base plus récente que le programme
            storage.open_user_db(path, ["CREATE TABLE a (x);"])
        with self.assertRaises(PermissionError):
            storage.open_user_db(os.path.join(self.src, "z.sqlite3"), [""], protected=[self.src])

    def test_source_db_copy_never_touches_original(self):
        db = os.path.join(self.src, "lib.db")
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (v)")
        con.execute("INSERT INTO t VALUES ('donnée du journal')")
        con.commit()          # reste dans le journal WAL tant que la connexion est ouverte
        before = snapshot(self.src)
        with storage.SourceDBCopy(db, self.cache, [self.src]) as c:
            self.assertEqual(c.execute("SELECT v FROM t").fetchone()[0], "donnée du journal")
        self.assertEqual(diff(before, snapshot(self.src)), [])
        self.assertEqual(os.listdir(self.cache), [])        # copie temporaire supprimée
        con.close()

    def test_open_source_db_is_read_only(self):
        db = os.path.join(self.src, "ro.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE t (v)")
        c.commit()
        c.close()
        before = snapshot(self.src)
        ro = storage.open_source_db(db)
        with self.assertRaises(sqlite3.Error):
            ro.execute("INSERT INTO t VALUES (1)")
        ro.close()
        self.assertEqual(diff(before, snapshot(self.src)), [])


class MetadataTests(TmpCase):
    def test_extract_exif_gps(self):
        p = write_jpeg(os.path.join(self.src, "a.jpg"), seed=1, gps=(48.8584, 2.2945), iso=400)
        m = metadata.extract(p)
        self.assertEqual(m["status"], "ok")
        self.assertEqual((m["width"], m["height"]), (640, 420))
        self.assertEqual(m["make"], "Canon")
        self.assertEqual(m["iso"], 400)
        self.assertAlmostEqual(m["aperture"], 2.8)
        self.assertAlmostEqual(m["exposure"], 1 / 125)
        self.assertAlmostEqual(m["gps_lat"], 48.8584, places=3)
        self.assertAlmostEqual(m["gps_lon"], 2.2945, places=3)
        self.assertEqual(m["has_exif"], 1)
        self.assertIsNotNone(m["taken"])

    def test_extract_cases(self):
        p = write_jpeg(os.path.join(self.src, "noexif.jpg"), no_exif=True)
        self.assertEqual(metadata.extract(p)["has_exif"], 0)
        bad = os.path.join(self.src, "bad.jpg")
        open(bad, "wb").write(b"pas une image")
        self.assertEqual(metadata.extract(bad)["status"], "error")
        self.assertEqual(metadata.extract(os.path.join(self.src, "absent.jpg"))["status"], "missing")
        vid = os.path.join(self.src, "v.mov")
        open(vid, "wb").write(b"\0" * 10)
        self.assertEqual(metadata.extract(vid)["status"], "video")
        # orientation 6 (rotation 90°) : dimensions affichées inversées
        ex = make_exif()
        ex[0x0112] = 6
        p6 = write_jpeg(os.path.join(self.src, "rot.jpg"), exif=ex)
        m6 = metadata.extract(p6)
        self.assertEqual((m6["width"], m6["height"]), (420, 640))

    def test_index_incremental_resume_and_readonly(self):
        paths = [write_jpeg(os.path.join(self.src, f"p{i}.jpg"), seed=i) for i in range(12)]
        before = snapshot(self.src)
        idx = metadata.MetadataIndex(os.path.join(self.cache, "metadata.sqlite3"), [self.src])
        items = [(f"k{i}", i, (lambda p=p: p)) for i, p in enumerate(paths)]
        # interruption après 5 fichiers (annulation depuis la fonction de résolution)
        cancel = threading.Event()
        calls = []

        def resolver(p):
            calls.append(p)
            if len(calls) == 6:
                cancel.set()
            return p
        prog = idx.update([(k, i, (lambda p=p: resolver(p))) for k, i, p in
                           [(f"k{i}", i, p) for i, p in enumerate(paths)]],
                          metadata.IndexProgress(), cancel, total=12, batch=2)
        self.assertTrue(prog.cancelled)
        n1 = idx.coverage()[0]
        self.assertGreaterEqual(n1, 4)
        # reprise : seuls les fichiers restants sont analysés
        prog2 = idx.update(items, metadata.IndexProgress(), threading.Event(), total=12)
        self.assertEqual(idx.coverage(), (12, 12))
        self.assertEqual(prog2.skipped, n1)
        # nouvelle passe : tout est ignoré ; fichier modifié : réanalysé
        prog3 = idx.update(items, metadata.IndexProgress(), threading.Event(), total=12)
        self.assertEqual(prog3.updated, 0)
        self.assertEqual(diff(before, snapshot(self.src)), [])
        time.sleep(1.1)
        write_jpeg(paths[0], seed=99, iso=1600)
        prog4 = idx.update(items, metadata.IndexProgress(), threading.Event(), total=12)
        self.assertEqual(prog4.updated, 1)
        self.assertEqual(idx.get("k0")["iso"], 1600)
        with self.assertRaises(PermissionError):
            metadata.MetadataIndex(os.path.join(self.src, "metadata.sqlite3"), [self.src])


class Annot:
    def __init__(self, fav=(), tags=None):
        self.fav, self.tags = set(fav), tags or {}

    def favorites(self):
        return self.fav

    def keys_with_tag(self, t):
        return {k for k, ts in self.tags.items() if t in ts}

    def tagged_keys(self):
        return {k for k, ts in self.tags.items() if ts}


def build_index(path: str, n: int = 40):
    """Index minimal au format d'iphotor (tables photos, albums, album_photos)."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE photos (id INTEGER PRIMARY KEY, guid TEXT, caption TEXT, comment TEXT, rating INTEGER,
            roll_id INTEGER, media_type TEXT, image_path TEXT, original_path TEXT, thumb_path TEXT,
            date REAL, mod_date REAL, filename TEXT, event_name TEXT, search TEXT);
        CREATE TABLE albums (id INTEGER PRIMARY KEY, name TEXT, name_norm TEXT, kind TEXT, raw_type TEXT,
            parent_id INTEGER, position INTEGER, photo_count INTEGER, date REAL, roll_id INTEGER, guid TEXT,
            year INTEGER, first_date REAL);
        CREATE TABLE album_photos (album_id INTEGER, position INTEGER, photo_id INTEGER);
    """)
    for i in range(1, n + 1):
        d = dt.datetime(2010 + i % 5, 1 + i % 12, 1 + i % 28, 12).timestamp() - 978307200
        con.execute("INSERT INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, f"G{i}", "Plage" if i % 4 == 0 else "", "Été" if i % 6 == 0 else "", i % 6, None,
                     "Movie" if i == 7 else "Image", f"/x/IMG_{i}.JPG", "", "", d, d,
                     f"IMG_{i}.{'CR2' if i % 10 == 0 else 'JPG'}", "Vacances" if i < 20 else "Noël", ""))
    con.execute("INSERT INTO albums VALUES (1,'Sélection','selection','album','Regular',NULL,1,3,NULL,NULL,'A1',NULL,NULL)")
    for pos, pid in enumerate((3, 5, 9)):
        con.execute("INSERT INTO album_photos VALUES (1, ?, ?)", (pos, pid))
    con.commit()
    return con


class SearchTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.con = build_index(os.path.join(self.cache, "index.sqlite3"))
        md = metadata.MetadataIndex(os.path.join(self.cache, "metadata.sqlite3"))
        c = md.connect()
        rows = []
        for i in range(1, 41):
            rows.append(metadata.MetadataIndex._row({
                "status": "ok", "path": f"/x/{i}", "file_size": i * 1_000_000, "width": 4000 if i % 3 else 3000,
                "height": 3000 if i % 3 else 4000, "has_exif": 0 if i == 40 else 1,
                "make": "Canon" if i % 2 else "NIKON CORPORATION", "model": "Canon EOS 450D" if i % 2 else "NIKON D7000",
                "iso": 100 * (1 + i % 8), "focal": 18.0 + i, "aperture": 2.8, "exposure": 1 / 125,
                "gps_lat": 45.0 if i % 5 == 0 else None, "gps_lon": 6.0 if i % 5 == 0 else None,
                "taken": None}, f"G{i}", i))
        metadata.MetadataIndex._write(c, rows[:-5])         # 5 photos non analysées
        c.close()
        search.attach_meta(self.con, md.path)
        self.annot = Annot(fav={"G2", "G4", "G6", "G8"}, tags={"G2": ["mer"], "G4": ["mer", "famille"], "G9": ["famille"]})

    def run_q(self, rules, match="all"):
        ids, warn = search.run(self.con, {"match": match, "rules": rules}, search.LIBRARY, self.annot)
        return ids

    def test_text_and_general(self):
        self.assertEqual(len(self.run_q([{"field": "event", "op": "equals", "value": "noel"}])), 21)
        self.assertEqual(sorted(self.run_q([{"field": "raw", "op": "yes"}])), [10, 20, 30, 40])
        self.assertEqual(self.run_q([{"field": "media", "op": "is", "value": "video"}]), [7])
        self.assertEqual(sorted(self.run_q([{"field": "album", "op": "contains", "value": "selec"}])), [3, 5, 9])
        self.assertEqual(sorted(self.run_q([{"field": "favorite", "op": "yes"}])), [2, 4, 6, 8])
        self.assertEqual(sorted(self.run_q([{"field": "tag", "op": "has", "value": "mer"}])), [2, 4])
        self.assertEqual(len(self.run_q([{"field": "tag", "op": "none"}])), 37)
        self.assertEqual(sorted(self.run_q([{"field": "caption", "op": "contains", "value": "PLAGE"}])),
                         [4, 8, 12, 16, 20, 24, 28, 32, 36, 40])

    def test_dates(self):
        y = self.run_q([{"field": "date", "op": "year", "value": "2012"}])
        self.assertTrue(y and all(i % 5 == 2 for i in y))
        b = self.run_q([{"field": "date", "op": "between", "value": "2011-01-01", "value2": "31/12/2011"}])
        self.assertEqual(sorted(b), sorted(i for i in range(1, 41) if 2010 + i % 5 == 2011))
        self.assertEqual(self.run_q([{"field": "date", "op": "on", "value": "2011-02-02"}]), [1])

    def test_exif_and_combination(self):
        canon = self.run_q([{"field": "make", "op": "contains", "value": "canon"}])
        self.assertTrue(all(i % 2 == 1 for i in canon))
        self.assertNotIn(37, canon)          # non analysée : jamais incluse
        r = self.run_q([{"field": "iso", "op": "ge", "value": "500"},
                        {"field": "orientation", "op": "is", "value": "portrait"},
                        {"field": "favorite", "op": "no"}])
        for i in r:
            self.assertTrue(100 * (1 + i % 8) >= 500 and i % 3 == 0 and i not in (2, 4, 6, 8))
        self.assertEqual(sorted(self.run_q([{"field": "gps", "op": "yes"}])), [5, 10, 15, 20, 25, 30, 35])
        self.assertEqual(self.run_q([{"field": "file_size", "op": "between", "value": "2,5", "value2": "3.5"}]), [3])
        self.assertEqual(len(self.run_q([{"field": "exposure", "op": "eq", "value": "1/125"}])), 35)
        ou = self.run_q([{"field": "favorite", "op": "yes"}, {"field": "raw", "op": "yes"}], match="any")
        self.assertEqual(sorted(ou), [2, 4, 6, 8, 10, 20, 30, 40])
        # combinaison période + appareil + favoris + tags (critère du cahier des charges)
        r = self.run_q([{"field": "date", "op": "between", "value": "2010-01-01", "value2": "2014-12-31"},
                        {"field": "model", "op": "contains", "value": "d7000"},
                        {"field": "favorite", "op": "yes"}, {"field": "tag", "op": "has", "value": "famille"}])
        self.assertEqual(r, [4])

    def test_errors_and_unavailable(self):
        with self.assertRaises(search.CriteriaError):
            self.run_q([{"field": "iso", "op": "ge", "value": "beaucoup"}])
        with self.assertRaises(search.CriteriaError):
            self.run_q([{"field": "date", "op": "on", "value": "32/13/2020"}])
        c = search.compile_criteria({"rules": [{"field": "event", "op": "contains", "value": "x"}]}, search.FOLDER)
        self.assertEqual(c.where, "(0)" if c.where.startswith("(") else "0")
        self.assertTrue(c.warnings)
        self.assertIn("Fabricant contient canon", search.describe(
            {"rules": [{"field": "make", "op": "contains", "value": "canon"}]}))

    def test_folder_dialect(self):
        class P:
            def __init__(self, i):
                self.id, self.filename, self.folder, self.size = i, f"f{i}.jpg", "a" if i < 3 else "b", i * 10
                self.date, self.media_type = None, "Image"
        con = sqlite3.connect(":memory:")
        search.attach_meta(con, "")
        search.folder_table(con, [P(i) for i in range(6)], lambda p: f"/d/f{p.id}.jpg")
        ids, _ = search.run(con, {"rules": [{"field": "folder", "op": "equals", "value": "b"}]}, search.FOLDER,
                            Annot(fav={"/d/f4.jpg"}))
        self.assertEqual(ids, [3, 4, 5])
        ids, _ = search.run(con, {"rules": [{"field": "favorite", "op": "yes"}]}, search.FOLDER, Annot(fav={"/d/f4.jpg"}))
        self.assertEqual(ids, [4])


class TimelineTests(unittest.TestCase):
    def test_build(self):
        ts = lambda *a: dt.datetime(*a).timestamp()
        items = [(1, ts(2010, 5, 3, 10)), (2, ts(2010, 5, 3, 11)), (3, ts(2010, 7, 1)), (4, ts(2013, 1, 1)),
                 (5, None), (6, ts(1850, 1, 1)), (7, ts(2010, 5, 4))]
        t = timeline.Timeline.build(items)
        self.assertEqual(t.total, 7)
        self.assertEqual(t.unknown, 2)
        yp = t.year_periods()
        self.assertEqual([p.level for p in yp], ["year", "gap", "year", "unknown"])
        self.assertEqual(yp[1].gap_years, 2)
        self.assertEqual(yp[0].count, 4)
        self.assertEqual(yp[0].first_id, 1)
        mp = t.month_periods(2010)
        self.assertEqual(len(mp), 12)
        self.assertEqual(mp[4].count, 3)
        self.assertEqual(mp[0].count, 0)
        dp = t.day_periods(2010, 5)
        self.assertEqual([(p.day, p.count) for p in dp], [(3, 2), (4, 1)])
        self.assertEqual(t.richest_days(1)[0].day, 3)
        self.assertEqual(mp[4].label, "Mai 2010")
        self.assertTrue(dp[0].start <= ts(2010, 5, 3, 10) < dp[0].end)


class CollectionsTests(TmpCase):
    def test_crud_order_relocate(self):
        col = collections.Collections(os.path.join(self.cache, "c.sqlite3"))
        a = col.create("Voyages")
        files = [write_jpeg(os.path.join(self.src, f"i{i}.jpg"), seed=i) for i in range(4)]
        refs = [{"scope": "fichiers", "key": p, "path": p, "filename": os.path.basename(p),
                 "size": os.path.getsize(p), "date": None} for p in files]
        self.assertEqual(col.add(a, refs), 4)
        self.assertEqual(col.add(a, refs[:2]), 0)          # doublons ignorés
        col.move(a, [("fichiers", files[3])], -3)
        self.assertEqual([i.key for i in col.items(a)][0], files[3])
        col.remove(a, [("fichiers", files[0])])
        self.assertEqual(len(col.items(a)), 3)
        self.assertEqual([i.position for i in col.items(a)], [1, 2, 3])
        col.rename(a, "Voyages 2010")
        self.assertEqual(col.get(a).name, "Voyages 2010")
        # disque « déplacé » : références manquantes, puis relocalisation
        moved = os.path.join(self.tmp, "deplace")
        shutil.move(self.src, moved)
        self.assertEqual(len(col.missing_refs()), 3)
        self.assertEqual(col.relocate(a, self.src, moved), 3)
        self.assertEqual(col.missing_refs(), [])
        # recherche par nom + taille
        shutil.move(moved, os.path.join(self.tmp, "ailleurs"))
        self.assertEqual(col.find_by_name(a, self.tmp), 3)
        self.assertEqual(col.missing_refs(), [])
        # suppression d'un album : les fichiers restent
        col.delete(a)
        self.assertTrue(all(os.path.isfile(os.path.join(self.tmp, "ailleurs", os.path.basename(f)))
                            for f in files))
        s = col.save_search("Canon", {"rules": [{"field": "make", "op": "contains", "value": "canon"}]})
        self.assertEqual(col.searches()[0][1], "Canon")
        col.delete_search(s)
        smart = col.create("RAW", "smart", {"rules": [{"field": "raw", "op": "yes"}]})
        self.assertEqual(col.albums("smart")[0].criteria["rules"][0]["field"], "raw")
        col.close()


class DuplicatesTests(TmpCase):
    def test_exact_near_and_distinct(self):
        a = write_jpeg(os.path.join(self.src, "a.jpg"), seed=5, size=(800, 600))
        shutil.copyfile(a, os.path.join(self.src, "a_copie.jpg"))
        img = scene(800, 600, seed=5)
        img.resize((400, 300)).save(os.path.join(self.src, "a_petite.jpg"), quality=60)
        write_jpeg(os.path.join(self.src, "b.jpg"), seed=77, size=(800, 600))
        write_jpeg(os.path.join(self.src, "c.jpg"), seed=123, size=(800, 600))
        before = snapshot(self.src)
        entries = [duplicates.Entry(key=p, path=p) for p in sorted(
            os.path.join(self.src, f) for f in os.listdir(self.src))]
        bad = duplicates.hash_entries(entries, True, lambda d, n: None, threading.Event())
        self.assertEqual(bad, 0)
        rep = duplicates.find_groups(entries)
        levels = {g.level: g for g in rep.groups}
        self.assertIn(duplicates.IDENTICAL, levels)
        self.assertEqual({os.path.basename(e.path) for e in levels[duplicates.IDENTICAL].entries},
                         {"a.jpg", "a_copie.jpg"})
        near = [g for g in rep.groups if g.level != duplicates.IDENTICAL]
        self.assertTrue(near, "l'image redimensionnée devrait être détectée")
        names = {os.path.basename(e.path) for e in near[0].entries}
        self.assertIn("a_petite.jpg", names)
        self.assertNotIn("b.jpg", names)
        self.assertNotIn("c.jpg", names)
        self.assertEqual(diff(before, snapshot(self.src)), [])
        g2 = duplicates.Group.from_json(json.loads(json.dumps(rep.groups[0].to_json())))
        self.assertEqual(len(g2.entries), len(rep.groups[0].entries))

    def test_grouping_exhaustive_vs_brute_force(self):
        import random
        rnd = random.Random(3)
        ents = []
        for i in range(3000):
            h = rnd.getrandbits(64)
            ents.append(duplicates.Entry(key=f"k{i}", path=f"k{i}", sha256=f"{i:064x}", phash=f"{h:016x}"))
            if i % 100 == 0:          # quasi-doublon planté à distance 1 à 6
                d = 1 + (i // 100) % 6
                bits = rnd.sample(range(64), d)
                v = h
                for b in bits:
                    v ^= 1 << b
                ents.append(duplicates.Entry(key=f"n{i}", path=f"n{i}", sha256=f"n{i:063x}", phash=f"{v:016x}"))
        rep = duplicates.find_groups(ents, similar=False)
        found = {frozenset(e.key for e in g.entries) for g in rep.groups}
        ints = [(e.key, int(e.phash, 16)) for e in ents]
        brute = set()
        for a in range(len(ints)):
            for b in range(a + 1, len(ints)):
                if bin(ints[a][1] ^ ints[b][1]).count("1") <= duplicates.NEAR_MAX:
                    brute.add(frozenset((ints[a][0], ints[b][0])))
        for pair in brute:
            self.assertTrue(any(pair <= g for g in found), pair)
        self.assertGreaterEqual(len(brute), 30)

    def test_series_window(self):
        base = int("0f0f0f0f0f0f0f0f", 16)
        ents = [duplicates.Entry(key="a", path="a", sha256="1", phash=f"{base:016x}", date=1000.0),
                duplicates.Entry(key="b", path="b", sha256="2", phash=f"{base ^ 0x3FF:016x}", date=1010.0),
                duplicates.Entry(key="c", path="c", sha256="3", phash=f"{base ^ 0x3FF:016x}", date=None)]
        rep = duplicates.find_groups(ents)
        sim = [g for g in rep.groups if g.level == duplicates.SIMILAR]
        self.assertEqual(len(sim), 1)                     # a et b (10 bits, même série)
        self.assertEqual({e.key for e in sim[0].entries} >= {"a", "b"}, True)

    def test_distance(self):
        self.assertEqual(duplicates.distance("0" * 16, "f" * 16), 64)
        self.assertEqual(duplicates.distance("0000000000000001", "0000000000000003"), 1)


class StatsExportTests(TmpCase):
    def records(self):
        base = dt.datetime(2019, 3, 10).timestamp()
        recs = []
        for i in range(10):
            recs.append({"filename": f"IMG_{i}.{'CR2' if i < 2 else 'JPG'}", "media_type": "Image",
                         "date": base + i * 86400 * 40 if i != 9 else None, "status": "ok" if i < 8 else None,
                         "file_size": 2_000_000, "width": 4000, "height": 3000, "has_exif": 1 if i < 7 else 0,
                         "make": "Canon", "model": "Canon EOS 450D", "lens": "EF50mm", "iso": 200 * (i + 1),
                         "focal": 50.0, "gps_lat": 45.0 if i < 2 else None, "gps_lon": 6.0 if i < 2 else None,
                         "caption": "Légende ; avec \"guillemets\"", "tags": ["mer", "été"], "favorite": i == 0,
                         "path": f"/p/IMG_{i}"})
        recs.append({"filename": "clip.mov", "media_type": "Movie", "date": base})
        return recs

    def test_stats(self):
        s = stats.compute(self.records())
        self.assertEqual(s["total"], 11)
        self.assertEqual(s["videos"], 1)
        self.assertEqual(s["raw"], 2)
        self.assertEqual(s["no_date"], 1)
        self.assertEqual(s["analysed"], 8)
        self.assertEqual(s["without_exif"], 1)
        self.assertAlmostEqual(s["gps_pct"], 25.0)
        self.assertEqual(s["cameras"][0], ("Canon EOS 450D", 8))
        self.assertEqual(dict(s["iso"])["101–200"], 1)
        self.assertEqual(sum(c for _y, c in s["years"]), 10)
        self.assertEqual(stats.format_bytes(1536), "1,5 Ko")

    def test_export_csv_json(self):
        out_csv = os.path.join(self.tmp, "export.csv")
        n = export.write(out_csv, "csv", self.records(), protected=[self.src])
        self.assertEqual(n, 11)
        with open(out_csv, encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter=";"))
        self.assertEqual(rows[0][0], "Nom du fichier")
        hdr = rows[0]
        r1 = dict(zip(hdr, rows[1]))
        self.assertEqual(r1["Tags"], "mer, été")
        self.assertEqual(r1["Favori"], "oui")
        self.assertIn("guillemets", r1["Légende"])
        out_json = os.path.join(self.tmp, "export.json")
        export.write(out_json, "json", self.records(), fields=["filename", "iso", "gps_lat", "tags"])
        data = json.load(open(out_json, encoding="utf-8"))
        self.assertEqual(len(data["photos"]), 11)
        self.assertEqual(set(data["photos"][0]), {"filename", "iso", "gps_lat", "tags"})
        self.assertEqual(data["photos"][1]["iso"], 400)
        with self.assertRaises(PermissionError):
            export.write(os.path.join(self.src, "x.csv"), "csv", [], protected=[self.src])
        self.assertFalse([f for f in os.listdir(self.tmp) if f.endswith(".iphotor-part")])


class GeoFoldersDepsTests(TmpCase):
    def test_geo(self):
        pts = [geo.Point(f"k{i}", i, 48.85 + i * 0.001, 2.29, place="Paris" if i < 3 else "") for i in range(5)]
        pts += [geo.Point("x", 99, 43.30, 5.37)]
        cl = geo.clusters(pts, "secteur")
        self.assertEqual(len(cl), 2)
        self.assertEqual(cl[0].name, "Paris")
        self.assertIn("43,3000° N", cl[1].name)
        box = geo.bounds(pts)
        x, y = geo.project(48.85, 2.29, box, 800, 600)
        lat, lon = geo.unproject(x, y, box, 800, 600)
        self.assertAlmostEqual(lat, 48.85, places=6)
        self.assertAlmostEqual(lon, 2.29, places=6)
        self.assertTrue(geo.osm_url(1, 2).startswith("https://www.openstreetmap.org/"))

    def test_folders_snapshot(self):
        e1 = [("/d/a.jpg", "", "a.jpg", 100.0, 10), ("/d/b.jpg", "", "b.jpg", 100.0, 10)]
        folders.save_snapshot(self.cache, "/d", folders.entries_signature(e1), False)
        old, t, rec = folders.load_snapshot(self.cache, "/d")
        e2 = [("/d/a.jpg", "", "a.jpg", 200.0, 11), ("/d/c.jpg", "", "c.jpg", 1.0, 1)]
        d = folders.compare(old, folders.entries_signature(e2))
        self.assertEqual((d.added, d.removed, d.changed), (["/d/c.jpg"], ["/d/b.jpg"], ["/d/a.jpg"]))
        self.assertIn("1 nouvelle", d.summary())
        with self.assertRaises(PermissionError):
            folders.save_snapshot(self.src, self.src, {}, False)
        self.assertEqual(folders.remember(["/a", "/b"], "/b"), ["/b", "/a"])

    def test_deps(self):
        names = {d.name: d for d in deps.check()}
        self.assertTrue(names["Pillow"].available)
        self.assertTrue(names["Pillow"].required)
        self.assertTrue(deps.report_lines())


class AppleMetaTests(TmpCase):
    def test_photos_like_db(self):
        os.makedirs(os.path.join(self.src, "database"))
        db = os.path.join(self.src, "database", "photos.db")
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid TEXT, masterId INTEGER);
            CREATE TABLE RKMaster (modelId INTEGER PRIMARY KEY, uuid TEXT);
            CREATE TABLE RKKeyword (modelId INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE RKKeywordForVersion (modelId INTEGER PRIMARY KEY, versionId INTEGER, keywordId INTEGER);
            CREATE TABLE RKPerson (modelId INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE RKFace (modelId INTEGER PRIMARY KEY, personId INTEGER, imageModelId INTEGER);
            CREATE TABLE RKPlace (modelId INTEGER PRIMARY KEY, defaultName TEXT);
            CREATE TABLE RKPlaceForVersion (modelId INTEGER PRIMARY KEY, versionId INTEGER, placeId INTEGER);
            INSERT INTO RKVersion VALUES (1, 'V1', 10), (2, 'V2', 20);
            INSERT INTO RKMaster VALUES (10, 'M1'), (20, 'M2');
            INSERT INTO RKKeyword VALUES (1, 'Montagne'), (2, 'Lac');
            INSERT INTO RKKeywordForVersion VALUES (1, 1, 1), (2, 2, 1), (3, 2, 2);
            INSERT INTO RKPerson VALUES (1, 'Alice');
            INSERT INTO RKFace VALUES (1, 1, 20);
            INSERT INTO RKPlace VALUES (1, 'Annecy');
            INSERT INTO RKPlaceForVersion VALUES (1, 2, 1);
        """)
        con.commit()
        con.close()
        before = snapshot(self.src)
        res = applemeta.import_terms(self.src, self.cache, [])
        self.assertEqual(res.keywords, [("V1", "Montagne"), ("V2", "Lac"), ("V2", "Montagne")])
        self.assertEqual(res.persons, [("V2", "Alice")])
        self.assertEqual(res.places, [("V2", "Annecy")])
        self.assertEqual(diff(before, snapshot(self.src)), [])
        self.assertEqual(os.listdir(self.cache), [])

    def test_iphoto_faces_db_and_absent(self):
        apdb = os.path.join(self.src, "Database", "apdb")
        os.makedirs(apdb)
        c = sqlite3.connect(os.path.join(apdb, "Library.apdb"))
        c.executescript("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid TEXT, masterId INTEGER);"
                        "CREATE TABLE RKMaster (modelId INTEGER PRIMARY KEY, uuid TEXT);"
                        "INSERT INTO RKVersion VALUES (1,'G1',5); INSERT INTO RKMaster VALUES (5,'MU5');")
        c.commit()
        c.close()
        c = sqlite3.connect(os.path.join(apdb, "Faces.db"))
        c.executescript("CREATE TABLE RKFaceName (faceKey INTEGER, name TEXT);"
                        "CREATE TABLE RKDetectedFace (masterUuid TEXT, faceKey INTEGER);"
                        "INSERT INTO RKFaceName VALUES (7,'Bob'); INSERT INTO RKDetectedFace VALUES ('MU5', 7);")
        c.commit()
        c.close()
        res = applemeta.import_terms(self.src, self.cache, [])
        self.assertEqual(res.persons, [("G1", "Bob")])
        empty = os.path.join(self.tmp, "vide")
        os.makedirs(empty)
        self.assertTrue(applemeta.import_terms(empty, self.cache, []).notes)


if __name__ == "__main__":
    unittest.main()
