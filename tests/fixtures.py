"""Données de test : photothèque iPhoto de démonstration (images synthétiques) et dossier d'images.

Aucune photo réelle : tout est généré. Usage en ligne de commande : python3 fixtures.py <dossier>.
build_demo(dossier) renvoie (chemin de la bibliothèque, chemin du dossier d'images).
"""
import datetime as dt
import math
import os
import plistlib
import random
import shutil
import sys

from PIL import Image, ImageDraw, ImageFilter

APPLE = dt.datetime(2001, 1, 1)

PALETTES = [  # ciel haut, ciel bas, collines, soleil
    ((40, 90, 170), (250, 180, 120), (40, 70, 60), (255, 230, 170)),
    ((20, 30, 70), (200, 90, 110), (30, 30, 50), (255, 200, 140)),
    ((110, 170, 230), (210, 235, 250), (70, 130, 80), (255, 255, 230)),
    ((70, 120, 160), (180, 210, 220), (90, 110, 130), (250, 250, 250)),
    ((240, 150, 90), (250, 220, 150), (120, 70, 50), (255, 240, 200)),
    ((60, 40, 110), (240, 120, 90), (50, 40, 70), (255, 210, 120)),
    ((140, 200, 220), (230, 245, 240), (40, 110, 120), (255, 255, 240)),
]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def scene(w, h, k):
    pal = PALETTES[k % len(PALETTES)]
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(pal[0], pal[1], y / h))
    sx, sy = random.randint(w // 5, 4 * w // 5), random.randint(h // 6, h // 2)
    r = random.randint(h // 14, h // 7)
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=pal[3])
    for layer in range(3):
        base = h * (0.55 + 0.13 * layer)
        amp = h * random.uniform(0.04, 0.12)
        ph = random.uniform(0, 6)
        fr = random.uniform(1.5, 4)
        pts = [(0, h)] + [(x, base - amp * math.sin(ph + fr * x / w * math.pi)) for x in range(0, w + 8, 8)] + [(w, h)]
        col = lerp(pal[2], (10, 15, 20), layer * 0.3)
        d.polygon(pts, fill=col)
    return img.filter(ImageFilter.GaussianBlur(1))


def exif_for(i, when, gps=None):
    ex = Image.Exif()
    ex[0x010F] = ["Canon", "Nikon", "FUJIFILM", "SONY"][i % 4]
    ex[0x0110] = ["Canon EOS 450D", "NIKON D7000", "X-T20", "ILCE-6000"][i % 4]
    ex[0x0132] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub = ex.get_ifd(0x8769)
    sub[0x9003] = when.strftime("%Y:%m:%d %H:%M:%S")
    sub[0x829A] = (1, [60, 125, 250, 500][i % 4])
    sub[0x829D] = ([28, 40, 56, 80][i % 4], 10)
    sub[0x8827] = [100, 200, 400, 800][i % 4]
    sub[0x920A] = ([18, 35, 50, 85][i % 4], 1)
    if gps:
        g = ex.get_ifd(0x8825)

        def dms(v):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            return (float(d), float(m), round(((v - d) * 60 - m) * 60, 2))
        g[1], g[2] = ("N" if gps[0] >= 0 else "S"), dms(gps[0])
        g[3], g[4] = ("E" if gps[1] >= 0 else "W"), dms(gps[1])
    return ex


def tl(d):
    return (d - APPLE).total_seconds()


GPS_SPOTS = [(45.8992, 6.1294), (45.9237, 6.8694), (43.2965, 5.3698), (48.8584, 2.2945)]


def build_demo(out: str) -> tuple:
    random.seed(7)
    out = os.path.abspath(out)
    LIB = os.path.join(out, "iPhoto Library")
    FOLDER = os.path.join(out, "Images exemple")
    os.makedirs(os.path.join(LIB, "Masters"), exist_ok=True)
    events = [
        ("Week-end à la montagne", dt.datetime(2009, 2, 14, 10), 18),
        ("Bord de mer", dt.datetime(2009, 7, 22, 16), 24),
        ("Randonnée d'automne", dt.datetime(2010, 10, 3, 9), 20),
        ("Jardin au printemps", dt.datetime(2011, 4, 18, 14), 16),
        ("Vacances d'été", dt.datetime(2011, 8, 5, 11), 34),
        ("Coucher de soleil", dt.datetime(2012, 6, 30, 21), 14),
        ("Lac et forêt", dt.datetime(2012, 9, 9, 15), 22),
    ]
    master, rolls, pid = {}, [], 1
    captions = ["Vue depuis le col", "Matin calme", "Le vieux phare", "Sentier", "Reflets", "Fin de journée"]
    for rid, (name, start, n) in enumerate(events, start=1):
        keys = []
        for j in range(n):
            when = start + dt.timedelta(minutes=17 * j)
            rel = f"Masters/{when:%Y/%m/%d}/IMG_{1000 + pid:04d}.JPG"
            path = os.path.join(LIB, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            portrait = pid % 7 == 3
            w, h = (800, 1200) if portrait else (1200, 800)
            kind = "Image"
            if pid == 12:      # vidéo sans aperçu
                rel = rel.replace(".JPG", ".MOV")
                kind = "Movie"
            elif pid == 27:    # fichier manquant
                pass
            elif pid == 33:    # fichier illisible
                with open(path, "wb") as fh:
                    fh.write(b"not a jpeg")
            else:
                gps = GPS_SPOTS[(pid // 3) % len(GPS_SPOTS)] if pid % 3 == 0 else None
                if gps:
                    gps = (gps[0] + (pid % 7) * 0.002, gps[1] + (pid % 5) * 0.002)
                scene(w, h, pid).save(path, "JPEG", quality=82, exif=exif_for(pid, when, gps))
            master[str(pid)] = {
                "GUID": f"DEMO{pid:05d}", "MediaType": kind,
                "Caption": captions[pid % len(captions)] if pid % 5 == 0 else f"IMG_{1000 + pid:04d}",
                "Comment": "Photo de démonstration générée" if pid % 9 == 0 else "",
                "Rating": (pid % 6) if pid % 4 == 0 else 0, "Roll": rid,
                "ImagePath": os.path.join(LIB, rel), "OriginalPath": "",
                "DateAsTimerInterval": tl(when), "ModDateAsTimerInterval": tl(when),
            }
            keys.append(str(pid))
            pid += 1
        rolls.append({"RollID": rid, "RollName": name, "RollDateAsTimerInterval": tl(start),
                      "KeyList": keys, "ProjectUuid": f"ROLL{rid}"})
    allk = list(master)
    albums = [
        {"AlbumId": 1, "AlbumName": "Photos", "Album Type": "Master", "KeyList": allk, "GUID": "A1"},
        {"AlbumId": 10, "AlbumName": "Voyages", "Album Type": "Folder", "KeyList": [], "GUID": "A10"},
        {"AlbumId": 11, "AlbumName": "Montagne", "Album Type": "Regular", "Parent": 10,
         "KeyList": allk[0:18] + allk[58:70], "GUID": "A11"},
        {"AlbumId": 12, "AlbumName": "Littoral", "Album Type": "Regular", "Parent": 10,
         "KeyList": allk[18:42], "GUID": "A12"},
        {"AlbumId": 20, "AlbumName": "Sélection", "Album Type": "Regular", "KeyList": allk[::6], "GUID": "A20"},
        {"AlbumId": 21, "AlbumName": "Paysages panoramiques", "Album Type": "Regular",
         "KeyList": allk[5:40:3], "GUID": "A21"},
        {"AlbumId": 22, "AlbumName": "Les mieux notées", "Album Type": "Smart",
         "KeyList": [k for k in allk if master[k]["Rating"] >= 4], "GUID": "A22"},
        {"AlbumId": 23, "AlbumName": "Album vide", "Album Type": "Regular", "KeyList": [], "GUID": "A23"},
    ]
    for r in rolls:
        albums.append({"AlbumId": 100 + r["RollID"], "AlbumName": r["RollName"], "Album Type": "Event",
                       "GUID": r["ProjectUuid"], "KeyList": r["KeyList"]})
    with open(os.path.join(LIB, "AlbumData.xml"), "wb") as fh:
        plistlib.dump({"Archive Path": LIB, "Application Version": "9.6.1", "Master Image List": master,
                       "List of Rolls": rolls, "List of Albums": albums}, fh)

    for sub, n in (("", 9), ("Randonnées", 14), ("Randonnées/Alpes", 8), ("Famille", 11)):
        d = os.path.join(FOLDER, sub)
        os.makedirs(d, exist_ok=True)
        for j in range(n):
            scene(1000, 700, random.randint(0, 50)).save(os.path.join(d, f"photo_{j + 1:02d}.jpg"), quality=80,
                                                         exif=exif_for(j, dt.datetime(2020, 5, 1 + j % 28, 12)))
    # doublons pour les tests : copie exacte et version réduite d'une même image
    src = os.path.join(FOLDER, "photo_01.jpg")
    shutil.copyfile(src, os.path.join(FOLDER, "photo_01_copie.jpg"))
    with Image.open(src) as im:
        im.resize((500, 350)).save(os.path.join(FOLDER, "photo_01_reduite.jpg"), quality=70)
    return LIB, FOLDER


if __name__ == "__main__":
    lib, folder = build_demo(sys.argv[1])
    print(lib)
    print(folder)
