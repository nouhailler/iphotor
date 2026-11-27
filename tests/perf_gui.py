"""Ouverture d'une grande bibliothèque dans la vraie interface : temps jusqu'à la première page
(index déjà construit), temps de chargement des 100 vignettes, mémoire. python3 tests/perf_gui.py 50000"""
import os
import resource
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import perf  # noqa: E402  (crée l'environnement isolé et charge iphotor)
from helpers import write_jpeg  # noqa: E402

ip = perf.ip


def main():
    out = []
    pool = [write_jpeg(os.path.join(perf.TMP, "pool", f"p{i}.jpg"), seed=i, size=(1600, 1067)) for i in range(300)]
    for n in [int(a) for a in sys.argv[1:]] or [10000, 50000]:
        lib = perf.make_library(n, pool)
        lm = ip.LibraryManager(lib)
        cm = ip.CacheManager(lm)
        cm.ensure_dirs()
        cm.build()
        root = ip.tk.Tk()
        root.geometry("1400x860")
        t0 = time.perf_counter()
        app = ip.MainWindow(root, lm, cm, settings=ip.Settings())
        t_page = None
        while time.perf_counter() - t0 < 60:
            root.update()
            if t_page is None and app.page_photos:
                t_page = time.perf_counter() - t0
            if t_page is not None and len(app.tile_images) >= len(app.page_photos):
                break
            time.sleep(0.005)
        t_thumbs = time.perf_counter() - t0
        t1 = time.perf_counter()
        app.next_page()
        while len(app.tile_images) < len(app.page_photos) and time.perf_counter() - t1 < 30:
            root.update()
            time.sleep(0.005)
        t_next = time.perf_counter() - t1
        t2 = time.perf_counter()
        app.tree.selection_set("timeline")
        root.update()
        t_tl = time.perf_counter() - t2
        r = {"photos": n, "premiere_page_s": round(t_page, 2), "vignettes_page1_s": round(t_thumbs, 2),
             "page_suivante_avec_vignettes_s": round(t_next, 2), "chronologie_s": round(t_tl, 2),
             "rss_mo": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}
        print(r, flush=True)
        out.append(r)
        app.quit()
    perf.shutil.rmtree(perf.TMP, ignore_errors=True)


main()
