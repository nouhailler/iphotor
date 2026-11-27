"""Diagnostic des dépendances obligatoires et facultatives."""

from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass


@dataclass
class Dep:
    name: str
    role: str
    available: bool
    detail: str
    required: bool = False
    package: str = ""


def _module(name: str) -> tuple[bool, str]:
    try:
        m = importlib.import_module(name)
        return True, str(getattr(m, "__version__", getattr(m, "VERSION", "")) or "présent")
    except Exception as exc:
        return False, type(exc).__name__


def check() -> list[Dep]:
    out = []
    ok, v = _module("tkinter")
    out.append(Dep("Tkinter", "interface graphique", ok, v, True, "python3-tk"))
    ok, v = _module("PIL")
    out.append(Dep("Pillow", "lecture des images, EXIF, miniatures, empreintes", ok, v, True, "python3-pil"))
    ok, v = _module("PIL.ImageTk")
    out.append(Dep("Pillow ImageTk", "affichage des images", ok, v, True, "python3-pil.imagetk"))
    heif_gdk = False
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        heif_gdk = any("heif" in f.get_name().lower() or "avif" in f.get_name().lower()
                       for f in GdkPixbuf.Pixbuf.get_formats())
        gdk_detail = "chargeur HEIF présent" if heif_gdk else "GdkPixbuf présent, chargeur HEIF absent"
    except Exception as exc:
        gdk_detail = type(exc).__name__
    out.append(Dep("GdkPixbuf + heif-gdk-pixbuf", "affichage des HEIC", heif_gdk, gdk_detail, False,
                   "python3-gi gir1.2-gdkpixbuf-2.0 heif-gdk-pixbuf"))
    ok, v = _module("pillow_heif")
    out.append(Dep("pillow-heif", "HEIC (affichage et EXIF) via Pillow", ok, v, False, "python3-pillow-heif"))
    ok, v = _module("rawpy")
    out.append(Dep("rawpy", "développement des RAW sans aperçu intégré", ok, v, False, "pip : rawpy"))
    for tool, role, pkg in (("exiftool", "EXIF des HEIC et formats rares", "libimage-exiftool-perl"),
                            ("wl-copy", "copie d'image dans le presse-papiers (Wayland)", "wl-clipboard"),
                            ("xclip", "copie d'image dans le presse-papiers (X11)", "xclip"),
                            ("xdg-open", "ouvrir un fichier, un dossier, OpenStreetMap", "xdg-utils")):
        p = shutil.which(tool)
        out.append(Dep(tool, role, p is not None, p or "absent", False, pkg))
    return out


def report_lines() -> list[str]:
    lines = []
    for d in check():
        mark = "✔" if d.available else ("✘" if d.required else "–")
        need = "obligatoire" if d.required else "facultatif"
        lines.append(f"{mark} {d.name:<28} {need:<12} {d.role} ({d.detail})"
                     + ("" if d.available else f" — paquet : {d.package}"))
    return lines
