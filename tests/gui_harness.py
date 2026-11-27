"""Banc d'essai de l'interface : environnement isolé, démo générée, pilotage des fenêtres modales.

Toutes les données d'iphotor (préférences, cache, favoris, albums virtuels) sont redirigées vers
un dossier temporaire : les données réelles de l'utilisateur ne sont jamais touchées.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Harness:
    def __init__(self, app_path: str = os.path.join(ROOT, "iphotor.py"), keep: bool = False):
        self.tmp = tempfile.mkdtemp(prefix="iphotor-gui-")
        for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            os.environ[k] = os.path.join(self.tmp, k.lower())
        from fixtures import build_demo
        self.lib, self.folder = build_demo(os.path.join(self.tmp, "sources"))
        self.dest = os.path.join(self.tmp, "destination")
        os.makedirs(self.dest)
        spec = importlib.util.spec_from_file_location("iphotor_app", app_path)
        self.ip = importlib.util.module_from_spec(spec)
        sys.modules["iphotor_app"] = self.ip
        spec.loader.exec_module(self.ip)
        self.results: list[tuple[str, bool, str]] = []
        self.errors: list[str] = []
        self.answers: dict[str, str] = {}      # titre de messagebox -> bouton (ok, yes, no, cancel)
        self.root = None
        self.app = None
        self.keep = keep

    # -- application ---------------------------------------------------------------
    def start(self, library: str = "", geometry: str = "1400x860+20+20"):
        ip = self.ip
        st = ip.Settings()
        st.copy_dest = self.dest
        st.save()
        library = library or self.lib
        lm = ip.LibraryManager(library)
        cm = ip.CacheManager(lm)
        cm.ensure_dirs()
        self.root = ip.tk.Tk(className="iphotor")
        self.root.report_callback_exception = self._tk_error
        self.app = ip.MainWindow(self.root, lm, cm, settings=st)
        self.root.geometry(geometry)
        self.root.after(300, self._close_messageboxes)
        self.wait(8, lambda: self.app.photos is not None and bool(self.app.page_photos))
        return self.app

    def stop(self):
        if self.app is not None:
            try:
                self.app.quit()
            except Exception:
                traceback.print_exc()
        self.app = self.root = None

    def cleanup(self):
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _tk_error(self, exc, val, tb):
        self.errors.append(repr(val))
        traceback.print_exception(exc, val, tb)

    # -- outils --------------------------------------------------------------------
    def check(self, name: str, cond, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok, str(detail)))
        print(("OK   " if ok else "ÉCHEC") + " " + name + ("" if ok or not detail else f"  ({detail})"), flush=True)
        return ok

    def wait(self, sec: float = 0.4, until=None) -> bool:
        end = time.time() + sec
        while time.time() < end:
            self.root.update()
            if until is not None:
                try:
                    if until():
                        return True
                except Exception:
                    pass
            time.sleep(0.02)
        return bool(until()) if until else True

    def toplevels(self, prefix: str) -> list:
        """Fenêtres (même imbriquées : dialogue ouvert depuis un autre dialogue) dont le titre commence par prefix."""
        return [w for w in self.walk(self.root) if isinstance(w, self.ip.tk.Toplevel) and w.winfo_exists()
                and w.winfo_viewable() and w.title().startswith(prefix)]

    @staticmethod
    def walk(w) -> list:
        out = [w]
        for c in w.winfo_children():
            out += Harness.walk(c)
        return out

    def button(self, win, text: str):
        return next((w for w in self.walk(win) if w.winfo_class() in ("TButton", "TMenubutton")
                     and str(w.cget("text")) == text), None)

    def entries(self, win) -> list:
        return [w for w in self.walk(win) if w.winfo_class() == "TEntry"]

    def on_modal(self, prefix: str, action, delay: int = 500):
        """Exécute action(fenêtre) dès qu'un dialogue modal dont le titre commence par prefix apparaît."""
        def go(tries=[0]):
            wins = self.toplevels(prefix)
            if wins:
                try:
                    action(wins[0])
                except Exception:
                    traceback.print_exc()
            elif tries[0] < 100:
                tries[0] += 1
                self.root.after(100, go)
        self.root.after(delay, go)

    def _close_messageboxes(self):
        """Répond aux tk_messageBox (créées côté Tcl, invisibles pour winfo_children)."""
        try:
            for name in self.root.tk.splitlist(self.root.tk.call("winfo", "children", ".")):
                if "messagebox" in name or "__tk__" in name:
                    title = self.root.tk.call("wm", "title", name)
                    btn = self.answers.get(title, "ok")
                    for cand in (btn, "ok", "yes"):
                        try:
                            self.root.tk.call(f"{name}.{cand}", "invoke")
                            print(f"   [boîte « {title} » → {cand}]", flush=True)
                            break
                        except Exception:
                            continue
        except Exception:
            pass
        if self.root is not None:
            try:
                self.root.after(250, self._close_messageboxes)
            except Exception:
                pass

    def click_tile(self, i: int, state: int = 0):
        """Clic réel (événement Tk) au centre de la vignette i, avec modificateurs."""
        app = self.app
        x, y = app._tile_origin(i)
        cy = app.canvas.canvasy(0)
        app.canvas.event_generate("<Button-1>", x=x + 90, y=int(y - cy + 90), state=state)
        self.wait(0.15)

    def summary(self) -> tuple[int, int]:
        ok = sum(1 for r in self.results if r[1])
        return ok, len(self.results)
