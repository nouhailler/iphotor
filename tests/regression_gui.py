"""Non-régression des fonctionnalités 1.x dans l'interface (47 vérifications).

Lancement : python3 tests/regression_gui.py [chemin/vers/iphotor.py]
Environnement isolé (dossier temporaire) et photothèque de démonstration générée.
"""
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fixtures import build_demo  # noqa: E402

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    print("Aucun affichage : test ignoré.")
    sys.exit(0)
APP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(HERE), "iphotor.py")
ENV = tempfile.mkdtemp(prefix="iphotor-reg-")
for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
    os.environ[k] = os.path.join(ENV, k.lower())
DEST = os.path.join(ENV, "dest")
os.makedirs(DEST)
LIB, FOLDER = build_demo(os.path.join(ENV, "sources"))


def snapshot(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[p] = (hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns, st.st_size)
        out[d] = ("dir", len(_dirs), len(files))
    return out


before = {**snapshot(LIB), **snapshot(FOLDER)}

spec = importlib.util.spec_from_file_location("iphotor_app", APP)
ip = importlib.util.module_from_spec(spec)
sys.modules["iphotor_app"] = ip
spec.loader.exec_module(ip)
settings = ip.Settings()
settings.copy_dest = DEST
settings.save()
library = ip.LibraryManager(LIB)
cache = ip.CacheManager(library)
cache.ensure_dirs()
root = ip.tk.Tk(className="iphotor")
errors = []
root.report_callback_exception = lambda e, v, tb: (errors.append(repr(v)), traceback.print_exception(e, v, tb))
app = ip.MainWindow(root, library, cache, settings=settings)
root.geometry("1400x860+20+20")
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("OK   " if cond else "ÉCHEC") + " " + name + (f"  ({detail})" if detail and not cond else ""))


def wait(sec=0.5, until=None):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until else True


def toplevels(prefix):
    return [w for w in root.winfo_children() if isinstance(w, ip.tk.Toplevel) and w.winfo_exists()
            and w.winfo_viewable() and w.title().startswith(prefix)]


def answer_modal(prefix, action, delay=700):
    """Planifie une action dans le prochain dialogue modal (qui bloque dans wait_window)."""
    def go():
        wins = toplevels(prefix)
        if wins:
            action(wins[0])
        else:
            root.after(200, go)
    root.after(delay, go)


def find_button(win, text):
    stack = [win]
    while stack:
        w = stack.pop()
        try:
            if w.winfo_class() == "TButton" and w.cget("text") == text:
                return w
        except Exception:
            pass
        stack.extend(w.winfo_children())
    return None


def bindings(widget):
    return sorted(widget.bind())


def run():
    try:
        wait(6, until=lambda: app.photos is not None and app.page_photos)
        check("index construit, grille affichée", app.photos is not None and len(app.page_photos) == 100)
        check("100 photos par page, 2 pages", app.total == 148 and app.page_count() == 2,
              f"{app.total} / {app.page_count()}")
        wait(3, until=lambda: len(app.tile_images) >= 95)
        check("vignettes chargées en arrière-plan", len(app.tile_images) >= 95, str(len(app.tile_images)))
        st = [v.get("status") for v in app.tile_images.values() if isinstance(v, dict)]
        check("états vidéo / introuvable / illisible détectés",
              "video" in st and "error" in st and any(v.get("exists") is False for v in app.tile_images.values()))
        # Raccourcis : liste attendue (identique à la v1.4.0)
        expected_root = {"<Control-Key-c>", "<Control-Key-C>", "<Control-Key-s>", "<Control-Key-S>",
                         "f", "t", "i", "<Key-Prior>", "<Key-Next>"}
        rb = set(bindings(root))
        check("raccourcis de la fenêtre principale présents", expected_root <= rb, str(expected_root - rb))
        allb = set(root.bind_all())
        check("raccourcis globaux présents", {"<Control-Key-q>", "<Control-Key-o>", "<Control-Key-O>",
                                              "<Key-F5>", "<Control-Key-f>"} <= allb, str(allb))
        print("BINDINGS_ROOT", sorted(rb))
        print("BINDINGS_ALL", sorted(allb))
        # Pagination
        app.next_page()
        wait(0.5)
        check("page suivante", app.page_no == 1 and len(app.page_photos) == 48)
        root.event_generate("<Prior>")
        wait(0.3)
        check("touche Page préc.", app.page_no == 0)
        # Navigation arborescence
        app.tree.selection_set("a:12:flat")
        wait(0.6)
        check("album sélectionné", app.view.kind == "album" and app.total == 24, app.view.title)
        app.tree.selection_set("y:2011")
        wait(0.6)
        check("année d'événements", app.view.kind == "year" and app.total == 50, str(app.total))
        app.tree.selection_set("all")
        wait(0.6)
        # Recherche
        app.search_var.set("phare")
        app.do_search()
        wait(0.6)
        check("recherche", app.view.kind == "search" and app.total > 0 and app.tree.exists("search"),
              f"{app.view.kind} {app.total}")
        app.clear_search()
        wait(0.6)
        check("effacer la recherche", not app.tree.exists("search") and app.view.kind == "all")
        # Visionneuse
        app.open_viewer(3)
        wait(1.5, until=lambda: app.viewer.pil is not None)
        v = app.viewer
        check("visionneuse ouverte et image chargée", v.pil is not None and v.index == 3)
        check("photo courante mise en évidence dans la grille", app.current_index == 3)
        v.win.event_generate("<Right>")
        wait(0.3)
        check("visionneuse : photo suivante (→)", v.index == 4)
        v.next()
        v.prev()
        wait(0.2)
        check("visionneuse : précédente / suivante", v.index == 4)
        v.show_index(99)
        wait(0.5)
        v.next()
        wait(0.5)
        check("visionneuse : changement de page synchronisé", app.page_no == 1 and v.index == 100)
        vb = set(bindings(v.win))
        check("raccourcis de la visionneuse", {"<Key-Left>", "<Key-Right>", "<Key-Escape>", "<Control-Key-c>",
                                               "f", "t", "i", "<Control-Key-s>"} <= vb, str(vb))
        print("BINDINGS_VIEWER", sorted(vb))
        v.win.geometry("700x500")
        wait(0.6)
        check("visionneuse réduite : actions en icônes", v._compact is True)
        v.win.geometry("1300x800")
        wait(0.6)
        v.close()
        wait(0.2)
        check("fermeture de la visionneuse", not v.win.winfo_viewable() and app.current_index is None)
        app.prev_page()
        wait(0.5)
        # Favoris (raccourci F sur la vignette survolée)
        app._set_hover(0)
        p0 = app.page_photos[0]
        app._grid_shortcut("fav")
        wait(0.3)
        check("favori ajouté (F)", app.is_favorite(p0) and app.tree.exists("fav"))
        check("compteur des favoris", app.tree.set("fav", "n") == "1", app.tree.set("fav", "n"))
        app.tree.selection_set("fav")
        wait(0.5)
        check("vue « Mes favoris »", app.view.kind == "keys" and app.total == 1)
        app.tree.selection_set("all")
        wait(0.5)
        # Tags via le dialogue
        def fill_tags(w):
            for child in w.winfo_children():
                pass
            dlg_entry = [x for x in _walk(w) if x.winfo_class() == "TEntry"][0]
            dlg_entry.insert(0, "Montagne, Test")
            find_button(w, "Enregistrer").invoke()
        answer_modal("Tags", fill_tags)
        app.edit_tags(p0)
        wait(0.4)
        check("tags enregistrés", app.tags_of(p0) == ["Montagne", "Test"], str(app.tags_of(p0)))
        check("rubrique Tags dans l'arborescence", app.tree.exists("sec:tags") and app.tree.exists("tag:1"))
        # EXIF
        app.show_exif(app.page_photos[1])
        wait(2, until=lambda: bool(toplevels("Informations")))
        ex = toplevels("Informations")
        ok = bool(ex) and any(x.winfo_class() == "Treeview" and len(x.get_children()) >= 2 for x in _walk(ex[0]))
        check("fenêtre EXIF", ok)
        if ex:
            texts = []
            for x in _walk(ex[0]):
                if x.winfo_class() == "Treeview":
                    for it in x.get_children():
                        for sub in x.get_children(it):
                            texts.append(x.item(sub, "text"))
            check("EXIF : appareil lu", "Appareil" in texts, str(texts[:6]))
            ex[0].destroy()
        # Copie d'une photo
        answer_modal("Copier vers", lambda w: find_button(w, "Copier").invoke())
        app.copy_photo_to(app.page_photos[1])
        wait(3, until=lambda: os.listdir(DEST))
        wait(0.5)
        check("copie d'une photo", any(f.endswith(".JPG") for f in os.listdir(DEST)), str(os.listdir(DEST)))
        # Copie d'un album avec sous-dossier
        answer_modal("Copier vers", lambda w: find_button(w, "Copier").invoke())
        app.copy_tree_item("a:12:flat")
        wait(6, until=lambda: os.path.isdir(os.path.join(DEST, "Littoral"))
             and len(os.listdir(os.path.join(DEST, "Littoral"))) >= 22)
        n = len(os.listdir(os.path.join(DEST, "Littoral"))) if os.path.isdir(os.path.join(DEST, "Littoral")) else 0
        check("copie d'un album (sous-dossier)", n >= 22, str(n))
        wait(1)
        # Annulation d'une copie
        answer_modal("Copier vers", lambda w: (w.nametowidget(w.winfo_children()[0]) and
                                               find_button(w, "Copier").invoke()))
        holder = {}

        def cancel_progress():
            pr = toplevels("Copie en cours")
            if pr:
                b = find_button(pr[0], "Annuler")
                b.invoke()
                holder["cancelled"] = True
            elif not holder.get("cancelled"):
                root.after(30, cancel_progress)
        root.after(750, cancel_progress)
        app.copy_tree_item("all")
        wait(8, until=lambda: holder.get("cancelled") and not toplevels("Copie en cours"))
        wait(1.0)
        for w in toplevels("Copie"):
            w.destroy()
        parts = [f for d, _s, fs in os.walk(DEST) for f in fs if f.endswith(".iphotor-part")]
        check("annulation de copie (aucun fichier partiel)", holder.get("cancelled") and not parts, str(parts))
        # Copie vers la bibliothèque refusée
        task = ip.CopyTask([(app.page_photos[0], "")], lambda ph: ip.resolve_photo_file(app.library, ph),
                           LIB, app._protected_roots())
        try:
            task.check_destination()
            refused = False
        except OSError:
            refused = True
        check("copie dans la bibliothèque refusée", refused)
        # Thèmes
        app.set_theme("dark")
        wait(0.5)
        check("thème sombre", ip.COLORS["bg"] == "#171A21" and app.canvas.cget("bg").lower() == "#171a21"
              and settings.theme == "dark")
        app.set_theme("light")
        wait(0.5)
        check("retour au thème clair", ip.COLORS["bg"] == "#F5F6F8" and app.canvas.cget("bg").lower() == "#f5f6f8")
        # Redimensionnement
        for geo in ("900x560", "1100x700", "1920x1080", "1400x860"):
            root.geometry(geo)
            wait(0.8)
            cols = app._columns()
            vis = all(w.winfo_ismapped() for w in (app.search_entry, app.btn_search, app.btn_next, app.title_label))
            sw = app.search_entry.winfo_width()
            check(f"redimensionnement {geo} : recherche et pagination visibles", vis and sw > 60 and cols >= 1,
                  f"cols={cols} search={sw}")
        # Mode dossier
        app.mode_var.set(ip.MODE_FOLDER)
        app.set_mode(ip.MODE_FOLDER)
        root.after(300, lambda: None)
        app.open_folder(FOLDER)
        wait(5, until=lambda: app.photos is not None and app.total > 0)
        check("mode dossier : analyse", app.total == 11, str(app.total))
        app.recursive_var.set(True)
        app._on_recursive_toggle()
        wait(5, until=lambda: app.total == 44)
        check("mode dossier : sous-dossiers inclus", app.total == 44 and app.ftree.exists("f:Randonnées/Alpes"),
              str(app.total))
        check("mode dossier : options visibles", app.folder_opts.winfo_ismapped())
        app.search_var.set("photo_0")
        app.do_search()
        wait(0.5)
        check("mode dossier : recherche", 0 < app.total < 44, str(app.total))
        app.clear_search()
        app.sort_var.set(ip.FOLDER_SORTS[1][0])
        app._on_sort_change()
        wait(0.5)
        check("mode dossier : tri", app.page_photos and app.page_photos[0].filename >= app.page_photos[-1].filename)
        app._set_hover(0)
        app._grid_shortcut("fav")
        wait(0.3)
        check("mode dossier : favori", app.ftree.exists("fav") and app.ftree.set("fav", "n") == "1")
        app.mode_var.set(ip.MODE_LIBRARY)
        app.set_mode(ip.MODE_LIBRARY)
        wait(0.8)
        check("retour au mode bibliothèque", app.mode == ip.MODE_LIBRARY and app.total > 0)
        # Messages
        app._show_grid_message("Message d'essai", ip.COLORS["missing"])
        wait(0.2)
        check("message d'erreur dans la grille", bool(app.canvas.find_withtag("msg")))
        app.load_page()
        wait(0.3)
    except Exception:
        traceback.print_exc()
        check("exception pendant le test", False, traceback.format_exc(limit=2))
    finally:
        app.quit()


def _walk(w):
    out = [w]
    for c in w.winfo_children():
        out += _walk(c)
    return out


def close_messageboxes():
    """Valide automatiquement les boîtes « Copie terminée » (tk_messageBox, modales)."""
    try:
        for name in root.tk.splitlist(root.tk.call("winfo", "children", ".")):
            if "messagebox" in name and root.tk.call("wm", "title", name) == "Copie terminée":
                MSGS.append(name)
                root.tk.call(name + ".ok", "invoke")
    except Exception as exc:
        print("closer:", exc)
    root.after(300, close_messageboxes)


MSGS = []
root.after(300, close_messageboxes)


root.after(300, close_messageboxes)
root.after(300, run)
root.mainloop()
after = {**snapshot(LIB), **snapshot(FOLDER)}
check("LECTURE SEULE : bibliothèque et dossier strictement inchangés", before == after,
      str(set(after) ^ set(before) or [k for k in before if before[k] != after.get(k)][:3]))
check("aucune erreur Tk", not errors, str(errors[:3]))
fails = [r for r in results if not r[1]]
print(f"\nRÉSULTAT : {len(results) - len(fails)}/{len(results)} vérifications réussies")
shutil.rmtree(ENV, ignore_errors=True)
sys.exit(1 if fails else 0)
