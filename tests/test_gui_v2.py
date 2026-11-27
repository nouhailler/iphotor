"""Tests d'intégration d'iphotor 2 : parcours utilisateurs complets dans la vraie interface.

Lancement : python3 tests/test_gui_v2.py   (nécessite un affichage ; sinon le test est ignoré)
Scénarios A à E du cahier des charges + chronologie, statistiques, carte, diaporama, dossiers,
mots-clés Apple, diagnostic, thèmes — et contrôle de LECTURE SEULE de toutes les sources.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui_harness import Harness, display_available  # noqa: E402
from helpers import diff, snapshot  # noqa: E402


def run_all(h: Harness) -> None:
    ip = h.ip
    # Bibliothèque « sur disque externe » (copie de la démo) pour le scénario D
    disk = os.path.join(h.tmp, "disque_externe")
    shutil.copytree(os.path.dirname(h.lib), disk)
    disk_lib = os.path.join(disk, "iPhoto Library")
    sources = (os.path.dirname(h.lib), disk)
    before = snapshot(*sources)

    # ------------------------------------------------------------------ Scénario A
    app = h.start()
    h.check("A1 bibliothèque ouverte", app.total == 148, app.total)
    app.tree.item("sec:albums", open=True)
    app.tree.selection_set("a:12:flat")
    h.wait(1.0)
    h.check("A2 navigation dans un album", app.view.kind == "album" and app.total == 24)
    h.wait(2.0, lambda: len(app.tile_images) >= 20)
    h.click_tile(0, state=0x4)
    h.click_tile(2, state=0x4)
    h.click_tile(5, state=0x1)       # Maj+clic : plage 2 → 5
    h.check("A3 sélection Ctrl+clic et Maj+clic", len(app.selection) == 5, len(app.selection))
    h.check("A3 barre de sélection affichée", app.selbar.winfo_ismapped()
            and "5 photos" in app.selbar_count.cget("text"))
    h.check("A3 compteur permanent", "5 photos" in app.sel_label.cget("text"))
    h.check("A3 aucune visionneuse ouverte par les clics avec modificateur",
            app.viewer is None or not app.viewer.win.winfo_viewable())
    app.next_page() if app.page_count() > 1 else None
    app.tree.selection_set("all")
    h.wait(0.6)
    app.next_page()
    h.wait(0.6)
    h.click_tile(0, state=0x4)
    h.check("A3 sélection conservée sur plusieurs pages et vues", len(app.selection) == 6, len(app.selection))
    sel = app.selected_photos()

    def fill_tags(w):
        h.entries(w)[0].insert(0, "Voyage, Montagne")
        h.button(w, "Ajouter").invoke()
    h.on_modal("Ajouter des tags", fill_tags)
    app.batch_add_tags()
    h.wait(0.3)
    h.check("A4 tags ajoutés à toute la sélection", all(set(app.tags_of(p)) >= {"Voyage", "Montagne"} for p in sel))
    app.batch_favorite(True)
    h.check("A4 favoris groupés", all(app.is_favorite(p) for p in sel))

    def new_album(w):
        h.entries(w)[0].insert(0, "Sélection test")
        h.button(w, "Ajouter").invoke()
    h.on_modal("Ajouter à un album virtuel", new_album)
    app.add_to_virtual_album()
    h.wait(0.3)
    albums = app.collections.albums("manual")
    h.check("A5 album virtuel créé avec 6 références", len(albums) == 1 and albums[0].count == 6,
            [(a.name, a.count) for a in albums])
    keys = {app.photo_key(p) for p in sel}
    h.stop()
    # A6/A7 : fermer puis rouvrir
    app = h.start()
    tagged = [p for p in app.photos.page(ip.View("all"), 0, 200) if app.photo_key(p) in keys]
    h.check("A7 persistance des tags après redémarrage",
            len(tagged) == 6 and all("Voyage" in app.tags_of(p) for p in tagged))
    app.mode_var.set(ip.MODE_COLLECTIONS)
    app.set_mode(ip.MODE_COLLECTIONS)
    h.wait(0.6)
    aid = app.collections.albums("manual")[0].id
    h.check("A7 album virtuel présent dans Collections", app.ctree.exists(f"va:{aid}"))
    app.ctree.selection_set(f"va:{aid}")
    h.wait(1.5)
    h.check("A7 album virtuel affiché (références multi-sources)", app.view.kind == "valbum" and app.total == 6)
    h.wait(2, lambda: len(app.tile_images) >= 6)
    h.check("A7 vignettes de l'album virtuel", sum(1 for v in app.tile_images.values() if v.get("img")) >= 5)
    app.selection = {app.photo_key(p): p for p in app.page_photos[4:5]}
    app.valbum_move_selection(-4)
    h.check("A réorganisation d'un album virtuel", app.coll_browser.photos[0].filename == app.page_photos[0].filename
            and app.collections.items(aid)[0].filename == app.page_photos[0].filename)
    app.clear_selection()
    app._valbum_remove_one(app.page_photos[0])
    h.check("A retrait d'une référence (fichier intact)", len(app.collections.items(aid)) == 5)
    app.open_viewer(0)
    h.wait(1.5, lambda: app.viewer.pil is not None)
    h.check("A visionneuse depuis un album virtuel", app.viewer.pil is not None)
    app.viewer.close()
    app.mode_var.set(ip.MODE_LIBRARY)
    app.set_mode(ip.MODE_LIBRARY)
    h.wait(0.8)

    # ------------------------------------------------------------------ Scénario B
    done = {}
    app.analyze_metadata(done=lambda: done.setdefault("ok", True))
    h.wait(60, lambda: done.get("ok"))
    total, analysed, with_exif = app.metadata_coverage()
    h.check("B0 analyse des métadonnées (incrémentale)", analysed == 145, (total, analysed, with_exif))
    done.clear()
    t0 = time.time()
    app.analyze_metadata(done=lambda: done.setdefault("ok", True))
    h.wait(30, lambda: done.get("ok"))
    h.check("B0 seconde analyse : rien à relire", "0 fichier(s) analysé(s)" in app.status_var.get(),
            app.status_var.get())
    app.open_advanced_search()
    dlg = app._adv_dialog
    h.check("B1 fenêtre de recherche avancée", dlg is not None and dlg.win.winfo_exists())
    crit = {"match": "all", "rules": [
        {"field": "date", "op": "between", "value": "2009-01-01", "value2": "2011-12-31"},
        {"field": "model", "op": "contains", "value": "D7000"},
        {"field": "favorite", "op": "yes"}]}
    dlg.load(crit)
    h.check("B2/B3/B4 critères chargés dans le formulaire", dlg.criteria() == crit, dlg.criteria())
    dlg.apply()
    h.wait(3, lambda: app.view.kind == "ids")
    con = sqlite3.connect(os.path.join(app.cache.cache_dir, "metadata.sqlite3"))
    nikon = {r[0] for r in con.execute("SELECT key FROM photo_meta WHERE model LIKE '%D7000%'")}
    con.close()
    fav = app.annotations.favorites(app._scope())
    exp = [p for p in app.photos.page(ip.View("all"), 0, 200)
           if app._key(p)[1] in nikon and app._key(p)[1] in fav and p.date is not None
           and 2009 <= time.localtime(p.date + 978307200).tm_year <= 2011]
    h.check("B5 résultats exacts (période + appareil + favoris)", app.view.kind == "ids"
            and sorted(app.view.ids) == sorted(p.id for p in exp), (app.total, len(exp)))
    h.check("B5 entrée « Recherche avancée » dans l'arborescence", app.tree.exists("adv"))

    def name_it(w):
        e = h.entries(w)[0]
        e.delete(0, "end")
        e.insert(0, "Nikon favoris")
        h.button(w, "Enregistrer").invoke()
    h.on_modal("Enregistrer la recherche", name_it)
    dlg.save_search()
    h.wait(0.3)
    saved = app.collections.searches()
    h.check("B6 recherche enregistrée", len(saved) == 1 and saved[0][1] == "Nikon favoris", saved)
    app.tree.selection_set("all")
    h.wait(0.5)
    app.tree.selection_set(f"saved:{saved[0][0]}")
    h.wait(3, lambda: app.view.kind == "ids")
    h.check("B7 réutilisation de la recherche enregistrée", sorted(app.view.ids) == sorted(p.id for p in exp))

    def smart_it(w):
        e = h.entries(w)[0]
        e.delete(0, "end")
        e.insert(0, "Photos RAW ou GPS")
        h.button(w, "Créer").invoke()
    dlg.load({"match": "any", "rules": [{"field": "gps", "op": "yes"}, {"field": "iso", "op": "ge", "value": "800"}]})
    h.on_modal("Album intelligent", smart_it)
    dlg.create_smart()
    h.wait(0.3)
    smart = app.collections.albums("smart")
    h.check("album intelligent créé", len(smart) == 1 and app.tree.exists(f"smart:{smart[0].id}"))
    app.tree.selection_set(f"smart:{smart[0].id}")
    h.wait(3, lambda: app.view.kind == "ids" and "intelligent" in app.view.title)
    n_smart = app.total
    con = sqlite3.connect(os.path.join(app.cache.cache_dir, "metadata.sqlite3"))
    exp_n = con.execute("SELECT COUNT(*) FROM photo_meta WHERE gps_lat IS NOT NULL OR iso >= 800").fetchone()[0]
    con.close()
    h.check("album intelligent : contenu calculé à l'ouverture", n_smart == exp_n, (n_smart, exp_n))
    dlg.win.destroy()
    # sélection d'une page entière + actions sur la vue
    app.tree.selection_set("all")
    h.wait(0.5)
    app.clear_selection()
    app.select_page()
    h.check("sélection de toute la page (Ctrl+A)", len(app.selection) == 100)
    app.clear_selection()
    h.check("désélection générale", not app.selection and not app.selbar.winfo_ismapped())

    # ------------------------------------------------------------------ Chronologie
    app.tree.selection_set("timeline")
    h.wait(1.0)
    h.check("chronologie : années + périodes vides", app.view.kind == "timeline"
            and [p.level for p in app.periods][:2] == ["year", "year"], [p.label for p in app.periods])
    y2011 = next(i for i, p in enumerate(app.periods) if p.level == "year" and p.year == 2011)
    app.open_period(app.periods[y2011])
    h.wait(0.6)
    h.check("chronologie : 12 mois de 2011", app.view.level == "months" and len(app.periods) == 12
            and sum(p.count for p in app.periods) == 50)
    aug = app.periods[7]
    app.open_period(aug)
    h.wait(0.6)
    h.check("chronologie : jours d'août 2011", app.view.level == "days" and app.periods
            and sum(p.count for p in app.periods) == aug.count)
    h.check("chronologie : bouton Retour visible", app.btn_back.winfo_ismapped())
    day = app.periods[0]
    app.open_period(day)
    h.wait(0.8)
    h.check("chronologie : photos du jour dans la grille", app.view.kind == "date" and app.total == day.count)
    app.timeline_back()
    h.wait(0.5)
    h.check("chronologie : retour aux jours", app.view.kind == "timeline" and app.view.level == "days")
    app.timeline_back()
    app.timeline_back()
    h.wait(0.5)
    h.check("chronologie : retour aux années", app.view.level == "years")
    h.wait(2, lambda: any(v.get("img") for v in app.tile_images.values()))
    h.check("chronologie : miniature représentative chargée", any(v.get("img") for v in app.tile_images.values()))

    # ------------------------------------------------------------------ Statistiques, carte, export, diaporama
    sw = app.open_stats() or None
    h.wait(3)
    stats_win = h.toplevels("Statistiques")
    st = None
    if stats_win:
        obj = [o for o in [getattr(app, "_last_stats", None)] if o]
    h.check("statistiques : fenêtre affichée", bool(stats_win))
    s = app._stats_cache.get("value")
    h.check("statistiques : indicateurs cohérents", s and s["total"] == 148 and s["videos"] == 1
            and s["analysed"] == 145 and s["gps"] > 0 and s["cameras"], s and (s["total"], s["videos"], s["analysed"]))
    for w in stats_win:
        w.destroy()
    app.open_map()
    h.wait(1.5)
    geo_win = h.toplevels("Carte")
    con = sqlite3.connect(os.path.join(app.cache.cache_dir, "metadata.sqlite3"))
    n_gps = con.execute("SELECT COUNT(*) FROM photo_meta WHERE gps_lat IS NOT NULL").fetchone()[0]
    con.close()
    h.check("carte : fenêtre et points GPS (hors ligne)", bool(geo_win) and n_gps > 10, n_gps)
    for w in geo_win:
        w.destroy()
    # Scénario E : export
    app.tree.selection_set("a:12:flat")
    h.wait(0.8)
    app.select_page()
    out_csv = os.path.join(h.dest, "export.csv")
    out_json = os.path.join(h.dest, "export.json")
    app.export_metadata("selection", "csv", [k for k, _ in ip.mexport.FIELDS], out_csv)
    h.wait(5, lambda: os.path.isfile(out_csv))
    app.export_metadata("selection", "json", ["filename", "model", "iso", "gps_lat", "tags", "favorite"], out_json)
    h.wait(5, lambda: os.path.isfile(out_json))
    rows = []
    if os.path.isfile(out_csv):
        with open(out_csv, encoding="utf-8") as fh:
            rows = list(csv.reader(fh, delimiter=";"))
    h.check("E2 export CSV (24 lignes + en-tête)", len(rows) == 25 and rows[0][0] == "Nom du fichier", len(rows))
    data = {}
    if os.path.isfile(out_json):
        with open(out_json, encoding="utf-8") as fh:
            data = json.load(fh)
    ph = data.get("photos", [])
    h.check("E3/E4 export JSON : champs choisis et valeurs EXIF", len(ph) == 24
            and set(ph[0]) == {"filename", "model", "iso", "gps_lat", "tags", "favorite"}
            and any(p["model"] for p in ph))
    for w in h.toplevels("Export"):
        w.destroy()
    app.start_slideshow("selection")
    h.wait(1.5)
    ss = h.toplevels("Diaporama")
    h.check("diaporama : ouvert sur la sélection", bool(ss))
    if ss:
        obj = next(o for o in [v for v in vars(app).values()] if False) if False else None
        ss[0].event_generate("<Right>")
        h.wait(0.5)
        ss[0].event_generate("<space>")
        h.wait(0.3)
        ss[0].event_generate("<Escape>")
        h.wait(0.5)
    h.check("diaporama : fermé par Échap", not h.toplevels("Diaporama"))
    app.compare_selection()                  # 24 sélectionnées : message d'information attendu
    app.clear_selection()
    app.selection = {app.photo_key(p): p for p in app.page_photos[:2]}
    app.compare_selection()
    h.wait(2.5)
    cmp_win = h.toplevels("Comparer")
    h.check("comparaison de deux photos", bool(cmp_win))
    for w in cmp_win:
        w.destroy()
    app.clear_selection()
    app.batch_properties() if False else None
    app.show_diagnostic()
    h.wait(0.5)
    diag = h.toplevels("Diagnostic")
    h.check("diagnostic", bool(diag))
    for w in diag:
        w.destroy()
    app.set_theme("dark")
    h.wait(0.5)
    app.set_theme("light")
    h.wait(0.3)

    # ------------------------------------------------------------------ Scénario C : doublons (dossier)
    app.mode_var.set(ip.MODE_FOLDER)
    app.set_mode(ip.MODE_FOLDER)
    app.open_folder(h.folder)
    h.wait(5, lambda: app.photos is not None and app.total > 0)
    app.recursive_var.set(True)
    app._on_recursive_toggle()
    h.wait(5, lambda: app.total == 44)
    h.check("C1 dossier analysé", app.total == 44, app.total)
    h.check("dossiers récents mémorisés", h.folder in app.settings.recent_folders)
    app.find_duplicates()
    h.wait(30, lambda: bool(h.toplevels("Doublons")) and not h.toplevels("Doublons — ") == [])
    h.wait(1)
    dw = h.toplevels("Doublons")
    dup = None
    if dw:
        for obj in h.walk(dw[0]):
            pass
    import gc
    for o in gc.get_objects():
        if isinstance(o, ip.DuplicatesWindow) and o.win.winfo_exists():
            dup = o
    groups = dup.report.groups if dup else []
    ident = [g for g in groups if g.level == "identique"]
    near = [g for g in groups if g.level == "quasi-identique"]
    names = lambda g: {os.path.basename(e.path) for e in g.entries}
    h.check("C2 doublons exacts", any(names(g) == {"photo_01.jpg", "photo_01_copie.jpg"} for g in ident),
            [names(g) for g in ident])
    h.check("C3 quasi-doublons (image réduite)", any("photo_01_reduite.jpg" in names(g) for g in near),
            [names(g) for g in near])
    if dup:
        gi = next(i for i, g in enumerate(groups) if g in ident)
        dup.tree.selection_set((f"g{gi}e0", f"g{gi}e1"))
        h.wait(0.3)
        dup.compare()
        h.wait(2)
        h.check("C4 comparaison depuis les doublons", bool(h.toplevels("Comparer")))
        for w in h.toplevels("Comparer"):
            w.destroy()
        dup.tree.selection_set((f"g{gi}e0",))
        h.on_modal("Copier vers", lambda w: h.button(w, "Copier").invoke())
        dup.copy_selected()
        h.wait(3, lambda: os.path.isfile(os.path.join(h.dest, "photo_01.jpg"))
               or os.path.isfile(os.path.join(h.dest, "photo_01_copie.jpg")))
        h.check("C5 copie de la version retenue", any(f.startswith("photo_01") for f in os.listdir(h.dest)))
        dup.win.destroy()
    h.check("doublons : résultats enregistrés et réouvrables", os.path.isfile(app._dup_results_path()))
    app.open_last_duplicates()
    h.wait(0.8)
    h.check("doublons : réouverture des derniers résultats", bool(h.toplevels("Doublons")))
    for w in h.toplevels("Doublons"):
        w.destroy()
    # recherche avancée en mode dossier + chronologie dossier
    app.run_advanced_search({"rules": [{"field": "folder", "op": "contains", "value": "alpes"}]})
    h.wait(3, lambda: app.view.title.startswith("Recherche avancée"))
    h.check("recherche avancée en mode dossier", app.total == 8, app.total)
    app.ftree.selection_set("timeline")
    h.wait(0.8)
    h.check("chronologie en mode dossier", app.view.kind == "timeline" and app.periods)
    # favoris de dossiers, instantanés, surveillance
    app.add_folder_favorite()
    h.check("dossier ajouté aux favoris", h.folder in app.settings.folder_favorites)
    # ------------------------------------------------------------------ Scénario D : disque externe
    h.stop()
    app = h.start(library=disk_lib)
    h.check("D1 bibliothèque sur disque externe ouverte", app.total == 148)
    unplugged = disk + ".debranche"
    os.rename(disk, unplugged)
    app._watch_disk()
    app._watch_disk()
    h.check("D3 déconnexion détectée", "déconnectée" in app.status_var.get(), app.status_var.get())
    h.check("D3 index toujours consultable", app.total == 148 and app.photos is not None)
    app.open_viewer(0)
    h.wait(2, lambda: app.viewer.canvas.find_all())
    h.check("D3 visionneuse : message clair au lieu d'une erreur", app.viewer.pil is None)
    app.viewer.close()
    os.rename(unplugged, disk)
    app._watch_disk()
    h.check("D5 reconnexion détectée", "de nouveau accessible" in app.status_var.get(), app.status_var.get())
    app.open_viewer(1)
    h.wait(2, lambda: app.viewer.pil is not None)
    h.check("D6 accès aux originaux rétabli", app.viewer.pil is not None)
    app.viewer.close()
    # ------------------------------------------------------------------ Mots-clés Apple (base synthétique)
    apdb = os.path.join(disk_lib, "Database", "apdb")
    os.makedirs(apdb)
    c = sqlite3.connect(os.path.join(apdb, "Library.apdb"))
    c.executescript("CREATE TABLE RKVersion (modelId INTEGER PRIMARY KEY, uuid TEXT, masterId INTEGER);"
                    "CREATE TABLE RKKeyword (modelId INTEGER PRIMARY KEY, name TEXT);"
                    "CREATE TABLE RKKeywordForVersion (modelId INTEGER PRIMARY KEY, versionId INTEGER, keywordId INTEGER);"
                    "INSERT INTO RKKeyword VALUES (1, 'Sommet');")
    for i in range(1, 11):
        c.execute("INSERT INTO RKVersion VALUES (?, ?, ?)", (i, f"DEMO{i:05d}", i))
        c.execute("INSERT INTO RKKeywordForVersion VALUES (?, ?, 1)", (i, i))
    c.commit()
    c.close()
    before_disk = snapshot(disk)
    app.import_apple_terms()
    h.wait(5, lambda: bool(h.toplevels("Informations Apple")))
    for w in h.toplevels("Informations Apple"):
        w.destroy()
    app.run_advanced_search({"rules": [{"field": "keyword", "op": "equals", "value": "sommet"}]})
    h.wait(3, lambda: app.view.kind == "ids")
    h.check("mots-clés Apple importés et cherchables", app.total == 10, app.total)
    h.check("mots-clés Apple : base source intacte", diff(before_disk, snapshot(disk)) == [])
    shutil.rmtree(os.path.join(disk_lib, "Database"))
    h.stop()
    h.check("LECTURE SEULE : toutes les sources strictement inchangées", diff(before, snapshot(*sources)) == [],
            diff(before, snapshot(*sources))[:5])
    h.check("aucune erreur Tk pendant les parcours", not h.errors, h.errors[:3])


class GuiV2Test(unittest.TestCase):
    @unittest.skipUnless(display_available(), "aucun affichage disponible")
    def test_parcours(self):
        h = Harness()
        try:
            run_all(h)
        except Exception:
            traceback.print_exc()
            h.check("exception pendant les parcours", False, traceback.format_exc(limit=3))
        finally:
            try:
                h.stop()
            except Exception:
                pass
            ok, n = h.summary()
            print(f"\nRÉSULTAT : {ok}/{n} vérifications réussies")
            h.cleanup()
        self.assertEqual(ok, n, [r for r in h.results if not r[1]])


if __name__ == "__main__":
    unittest.main()
